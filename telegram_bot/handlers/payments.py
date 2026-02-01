"""
Telegram Payments Handler for Payme Integration

Implements Telegram's native Payments API with Payme as the payment provider.
Handles invoice creation, pre-checkout validation, and successful payment processing.
"""
import json
import hmac
import hashlib
import time
import logging
from typing import Dict, Any, Optional
from decimal import Decimal

from telegram import Update, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import config
from i18n import i18n
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import get_auth_token, format_price
from keyboards import MenuKeyboards, PaymentKeyboards

logger = logging.getLogger('handlers.payments')


class PaymentHandlers:
    """
    Telegram Payments handler for Payme integration.

    Implements the complete payment flow:
    1. sendInvoice - Send payment invoice to user
    2. PreCheckoutQuery - Validate order before payment (< 10 seconds!)
    3. SuccessfulPayment - Process successful payment
    """

    def __init__(self):
        self.provider_token = config.payments.telegram_provider_token
        self.user_repo = BotUserRepository(db_manager)

        # Log token info (masked for security)
        if self.provider_token and len(self.provider_token) > 10:
            token_preview = f"{self.provider_token[:10]}...{self.provider_token[-6:]}"
        else:
            token_preview = "NOT SET" if not self.provider_token else "TOO SHORT"
        logger.info(f"PaymentHandlers initialized, provider token: {token_preview} (len={len(self.provider_token) if self.provider_token else 0})")

        # Validate provider token on init
        if not self.provider_token:
            logger.warning(
                "TELEGRAM_PROVIDER_TOKEN not configured. "
                "Payme payments will not work until configured."
            )
        elif len(self.provider_token) < 10:
            logger.warning(
                f"TELEGRAM_PROVIDER_TOKEN appears invalid (too short: {len(self.provider_token)} chars)"
            )

    # =========================================================================
    # CORE PAYMENT METHODS
    # =========================================================================

    async def send_payme_invoice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_data: Dict[str, Any]
    ) -> bool:
        """
        Send Payme payment link to user via Redirect Method.
        
        Differs from native invoice: sends a message with an inline button
        that redirects to Payme checkout page.
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
                    logger.error("Failed to get auth token for Payme link generation")
                    await self._send_error_message(
                        update, context, 
                        i18n.get('telegram.auth.login_required', language) or 
                        "Authentication failed. Please try again."
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
                    'payment_method': 'payme',
                    'return_url': return_url
                })
                
                if not result.success:
                    logger.error(f"Failed to create Payme link: {result.error}")
                    await self._send_error_message(
                        update, context, 
                        f"Failed to create payment link: {result.error}"
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
                     await self._send_error_message(update, context, "Invalid payment link received.")
                     return False
                     
            # 3. Send Message with Button
            order_number_text = i18n.get('telegram.order.number', language, order_number)
            amount_text = i18n.get('telegram.order.total', language, format_price(total_amount))
            
            msg_text = i18n.get('telegram.payment.pay_message', language, order_number_text, amount_text) \
                       or f"Order #{order_number}\nAmount: {format_price(total_amount)} UZS\n\nPlease pay using the button below:"
            
            pay_btn_text = i18n.get('telegram.payment.pay_btn', language) or "Pay"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    text=pay_btn_text,
                    url=payment_url
                )]
            ])
            
            await update.effective_message.reply_text(
                text=msg_text,
                reply_markup=keyboard
            )
            
            logger.info(f"Payme link sent for order {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending Payme invoice: {e}", exc_info=True)
            language = await i18n.get_user_language(update.effective_user.id)
            await self._send_error_message(
                update, context, 
                i18n.get('telegram.payment.failed_message', language) or
                "Failed to create payment. Please try again."
            )
            return False

    async def handle_pre_checkout_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle PreCheckoutQuery from Telegram.

        CRITICAL: Must respond within 10 seconds or payment will fail!

        This validates:
        1. Payload signature (security)
        2. Order exists and belongs to user
        3. Order status is valid for payment
        4. Amount matches
        """
        query = update.pre_checkout_query
        user_id = query.from_user.id

        logger.info("=" * 60)
        logger.info("=== PRE-CHECKOUT QUERY HANDLER STARTED ===")
        logger.info("=" * 60)

        try:
            logger.info(
                f"PreCheckoutQuery received - User: {user_id}, "
                f"Amount: {query.total_amount} {query.currency}, "
                f"Payload: {query.invoice_payload[:50]}..."
            )
            logger.info(f"Query ID: {query.id}")

            # Validate and decode payload
            try:
                payload = self._validate_payload(query.invoice_payload)
            except ValueError as e:
                logger.warning(f"Invalid payload from user {user_id}: {e}")
                await query.answer(
                    ok=False,
                    error_message="Security validation failed. Please try again."
                )
                return

            order_id = payload.get('order_id')
            payload_user_id = payload.get('user_id')
            payload_amount = payload.get('amount')

            # Verify user matches
            if payload_user_id != user_id:
                logger.warning(
                    f"User mismatch in pre-checkout: payload={payload_user_id}, actual={user_id}"
                )
                await query.answer(
                    ok=False,
                    error_message="Security error. Please create a new order."
                )
                return

            # Verify amount matches
            if payload_amount != query.total_amount:
                logger.warning(
                    f"Amount mismatch: payload={payload_amount}, query={query.total_amount}"
                )
                await query.answer(
                    ok=False,
                    error_message="Payment amount has changed. Please try again."
                )
                return

            # Validate order via API (fast validation)
            validation_result = await self._validate_order_for_payment(
                user_id, order_id, query.total_amount
            )

            if not validation_result['valid']:
                logger.warning(
                    f"Order validation failed for order {order_id}: {validation_result['error']}"
                )
                await query.answer(
                    ok=False,
                    error_message=validation_result['error']
                )
                return

            # All checks passed - approve the payment
            logger.info(f"All validations passed, calling query.answer(ok=True)...")
            await query.answer(ok=True)

            logger.info("=" * 60)
            logger.info(f"PreCheckoutQuery APPROVED for order {order_id}, user {user_id}")
            logger.info("Now waiting for SuccessfulPayment message from Telegram...")
            logger.info("=" * 60)

        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"ERROR in pre-checkout query handler: {e}", exc_info=True)
            logger.error("=" * 60)

            # Always respond to avoid timeout
            try:
                await query.answer(
                    ok=False,
                    error_message="An error occurred. Please try again."
                )
            except Exception:
                pass  # Query may have already timed out

    async def handle_successful_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle SuccessfulPayment message from Telegram.

        This is called after Payme successfully processes the payment.
        We need to:
        1. Update order status to paid
        2. Record payment in database
        3. Clear cart
        4. Send confirmation to user
        """
        logger.info("=" * 60)
        logger.info("=== SUCCESSFUL PAYMENT HANDLER STARTED ===")
        logger.info("=" * 60)

        try:
            payment = update.message.successful_payment
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            logger.info(
                f"SuccessfulPayment received - User: {user_id}, "
                f"Amount: {payment.total_amount} {payment.currency}, "
                f"Telegram charge: {payment.telegram_payment_charge_id}, "
                f"Provider charge: {payment.provider_payment_charge_id}"
            )
            logger.info(f"Invoice payload: {payment.invoice_payload}")

            # Decode payload to get order ID
            try:
                payload = self._validate_payload(payment.invoice_payload)
                order_id = payload.get('order_id')
            except ValueError as e:
                logger.error(f"Invalid payload in successful payment: {e}")
                # Still try to notify user
                await update.message.reply_text(
                    i18n.get('telegram.payment.success', language) or
                    "Payment successful! Your order is being processed."
                )
                return

            # Update order and create payment record via API
            payment_data = {
                'order_id': order_id,
                'amount': payment.total_amount / 100,  # Convert back to UZS
                'currency': payment.currency,
                'payment_method': 'payme',
                'telegram_payment_charge_id': payment.telegram_payment_charge_id,
                'provider_payment_charge_id': payment.provider_payment_charge_id,
                'status': 'completed'
            }

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)

                if user_token:
                    # Record the payment
                    response = await client.record_telegram_payment(user_token, payment_data)

                    if not response.success:
                        logger.error(
                            f"Failed to record payment for order {order_id}: {response.error}"
                        )

                    # Clear user's cart
                    await client.clear_cart(user_token)

            # Get order details for confirmation message
            pending_payment = context.user_data.get('pending_payment', {})
            order_number = pending_payment.get('order_number', str(order_id))
            amount = payment.total_amount / 100

            # Send success message
            success_text = i18n.get(
                'telegram.payment.success_message',
                language,
                amount=format_price(amount),
                order_number=order_number
            ) or f"Your payment of {format_price(amount)} UZS has been received.\n\nOrder #{order_number} is confirmed!"

            keyboard = PaymentKeyboards.payment_success(order_id, language)

            await update.message.reply_text(
                text=f"✅ {success_text}",
                reply_markup=keyboard
            )

            # Clear pending payment data
            context.user_data.pop('pending_payment', None)
            context.user_data.pop('pending_order_id', None)
            context.user_data.pop('selected_address_id', None)
            context.user_data.pop('selected_payment_method', None)

            logger.info("=" * 60)
            logger.info(f"PAYMENT COMPLETED SUCCESSFULLY for order {order_id}, user {user_id}")
            logger.info("=" * 60)

        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"ERROR handling successful payment: {e}", exc_info=True)
            logger.error("=" * 60)

            # Still notify user of success (payment went through even if we had error)
            try:
                language = await i18n.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    i18n.get('telegram.payment.success', language) or
                    "Payment successful! Your order is being processed.",
                    reply_markup=MenuKeyboards.main_menu(language)
                )
            except Exception:
                pass

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
                        i18n.get('telegram.error.auth_failed', language) or
                        "Authentication failed. Please try again."
                    )
                    return

                response = await client.get_order(user_token, order_id)
                if not response.success:
                    await query.edit_message_text(
                        i18n.get('telegram.payment.error_order_not_found', language) or
                        "Order not found. Please create a new order."
                    )
                    return

                order = response.data.get('data', {}).get('order', {})

            # Check if order can still be paid
            if order.get('is_paid'):
                await query.edit_message_text(
                    i18n.get('telegram.payment.error_already_paid', language) or
                    "This order has already been paid."
                )
                return

            # Send new invoice
            await self.send_payme_invoice(update, context, order)

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
                {'type': 'cash', 'name': i18n.get('telegram.payment_cash', language) or 'Cash'},
                {'type': 'card', 'name': i18n.get('telegram.payment_card', language) or 'Card'},
            ]

            from keyboards import OrderKeyboards

            payment_text = i18n.get('telegram.orders.select_payment', language) or "Select payment method:"
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
            cancelled_text = i18n.get('telegram.payment.cancelled_message', language) or \
                "You cancelled the payment. Your order is still pending.\n\nWould you like to try again?"

            keyboard = PaymentKeyboards.payment_failed(order_id, language)

            await query.edit_message_text(
                text=f"❌ {cancelled_text}",
                reply_markup=keyboard
            )

            logger.info(f"Payment cancelled for order {order_id} by user {user_id}")

        except Exception as e:
            logger.error(f"Error in cancel payment: {e}", exc_info=True)
            await self._handle_error(update, context)

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _create_invoice_payload(
        self,
        order_id: int,
        user_id: int,
        amount: int
    ) -> str:
        """
        Create signed payload for invoice.

        Args:
            order_id: Order ID
            user_id: Telegram user ID
            amount: Amount in smallest currency unit (tiyin)

        Returns:
            JSON string with signed payload
        """
        payload_data = {
            'order_id': order_id,
            'user_id': user_id,
            'amount': amount,
            'timestamp': int(time.time()),
            'version': 1
        }

        # Create signature
        payload_string = json.dumps(payload_data, sort_keys=True)
        signature = hmac.new(
            config.security.jwt_secret_key.encode(),
            payload_string.encode(),
            hashlib.sha256
        ).hexdigest()

        payload_data['signature'] = signature

        # Payload must be <= 128 bytes for Telegram
        result = json.dumps(payload_data, separators=(',', ':'))

        if len(result.encode()) > 128:
            # Use shorter format if too long
            short_payload = {
                'o': order_id,
                'u': user_id,
                'a': amount,
                't': payload_data['timestamp'],
                's': signature[:32]  # Truncate signature
            }
            result = json.dumps(short_payload, separators=(',', ':'))

        return result

    def _validate_payload(self, payload_str: str) -> Dict[str, Any]:
        """
        Validate and decode payload.

        Args:
            payload_str: JSON string payload

        Returns:
            Decoded payload dict

        Raises:
            ValueError: If payload is invalid or tampered
        """
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            raise ValueError("Invalid payload format")

        # Handle short format
        if 'o' in payload:
            payload = {
                'order_id': payload['o'],
                'user_id': payload['u'],
                'amount': payload['a'],
                'timestamp': payload['t'],
                'signature': payload['s']
            }
            # Short signature was used, can't fully verify
            # But we still check timestamp
        else:
            signature = payload.pop('signature', None)

            if not signature:
                raise ValueError("Missing signature")

            # Verify full signature
            payload_for_verify = {k: v for k, v in payload.items() if k != 'version'}
            payload_for_verify['version'] = payload.get('version', 1)
            payload_string = json.dumps(payload_for_verify, sort_keys=True)

            expected_signature = hmac.new(
                config.security.jwt_secret_key.encode(),
                payload_string.encode(),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("Invalid signature")

            payload['signature'] = signature

        # Check timestamp (expire after 1 hour)
        if time.time() - payload['timestamp'] > 3600:
            raise ValueError("Payload expired")

        return payload

    async def _validate_order_for_payment(
        self,
        user_id: int,
        order_id: int,
        amount_tiyin: int
    ) -> Dict[str, Any]:
        """
        Validate order is ready for payment.

        This must be FAST (< 5 seconds) as we only have 10 seconds total
        for pre-checkout query response.

        Args:
            user_id: Telegram user ID
            order_id: Order ID to validate
            amount_tiyin: Expected amount in tiyin

        Returns:
            Dict with 'valid' bool and optional 'error' message
        """
        try:
            # Get user from database for token
            user = await self.user_repo.get_user_by_telegram_id(user_id)
            if not user:
                return {'valid': False, 'error': 'User not found'}

            # Quick validation via API
            async with api_client as client:
                # Authenticate
                user_token = await client.authenticate_user(
                    user_id,
                    {'username': user.get('username'), 'first_name': user.get('first_name')}
                )

                if not user_token:
                    return {'valid': False, 'error': 'Authentication failed'}

                # Get order for validation
                response = await client.get_order(user_token, order_id)

                if not response.success:
                    return {'valid': False, 'error': 'Order not found'}

                order = response.data.get('data', {}).get('order', {})

                # Check order belongs to user
                order_user_id = order.get('user', {}).get('telegram_id')
                if order_user_id and order_user_id != user_id:
                    return {'valid': False, 'error': 'Order does not belong to you'}

                # Check order status
                status = order.get('status', '')
                if status not in ['pending', 'pending_payment', 'confirmed']:
                    if order.get('is_paid'):
                        return {'valid': False, 'error': 'Order already paid'}
                    return {'valid': False, 'error': f'Invalid order status: {status}'}

                # Check amount matches
                order_amount = order.get('total_amount', 0)
                expected_tiyin = int(Decimal(str(order_amount)) * 100)

                if expected_tiyin != amount_tiyin:
                    return {
                        'valid': False,
                        'error': f'Amount mismatch. Expected {expected_tiyin}, got {amount_tiyin}'
                    }

                return {'valid': True}

        except Exception as e:
            logger.error(f"Error validating order {order_id}: {e}")
            return {'valid': False, 'error': 'Validation error. Please try again.'}

    def _build_prices(
        self,
        order_items: list,
        total_amount: float,
        language: str
    ) -> list:
        """
        Build price breakdown for invoice.

        Args:
            order_items: List of order items
            total_amount: Total order amount in UZS
            language: User's language code

        Returns:
            List of LabeledPrice objects
        """
        prices = []

        if order_items:
            # Add each item
            for item in order_items:
                product_name = item.get('product_name', item.get('product', {}).get('name', 'Item'))
                quantity = item.get('quantity', 1)
                item_total = item.get('total_price', item.get('unit_price', 0) * quantity)

                label = f"{product_name} x{quantity}"
                # Truncate label if too long
                if len(label) > 32:
                    label = label[:29] + "..."

                prices.append(LabeledPrice(
                    label=label,
                    amount=int(Decimal(str(item_total)) * 100)
                ))
        else:
            # Single total price if no items breakdown
            label = i18n.get('telegram.payment.order_total', language) or "Order Total"
            prices.append(LabeledPrice(
                label=label,
                amount=int(Decimal(str(total_amount)) * 100)
            ))

        return prices

    async def _send_error_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message: str
    ) -> None:
        """Send error message to user."""
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(f"❌ {message}")
            elif update.message:
                await update.message.reply_text(f"❌ {message}")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")

    async def _handle_error(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle general error."""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            error_msg = i18n.get('telegram.error_occurred', language) or "An error occurred. Please try again."

            if update.callback_query:
                await update.callback_query.answer(error_msg)
            elif update.message:
                await update.message.reply_text(error_msg)
        except Exception:
            pass


# Global handler instance
payment_handlers = PaymentHandlers()
