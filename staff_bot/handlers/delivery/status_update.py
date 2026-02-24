"""
Delivery Status Update Handler for Staff Bot
Handles status transitions, delivered flow, failed flow, and cash collection.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.delivery import DeliveryKeyboards
from keyboards.common import CommonKeyboards
from utils.formatters import format_delivery_status, format_currency
from permissions import require_auth, require_delivery_driver
from i18n import i18n
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS

logger = logging.getLogger(__name__)

# Conversation state for cash input
CASH_INPUT = 100


class StatusUpdateHandler(BaseHandler):
    """Handle delivery status transitions"""

    @require_auth
    @require_delivery_driver
    async def initiate_status_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initiate a delivery status change (confirmation step)"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_status_{delivery_id}_{new_status}
            parts = query.data.split('_')
            delivery_id = int(parts[2])
            new_status = '_'.join(parts[3:])  # Handle statuses like 'in_transit'

            # Store pending status change
            context.user_data['pending_status'] = {
                'delivery_id': delivery_id,
                'new_status': new_status,
            }

            # Special handling for different statuses
            if new_status == 'failed':
                # Show reason selection
                keyboard = DeliveryKeyboards.failed_reasons(language, delivery_id)
                await query.edit_message_text(
                    i18n.get('staff.delivery.select_fail_reason', language),
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                return

            if new_status == 'delivered':
                delivery_info = context.user_data.get('current_delivery', {})
                payment_method = delivery_info.get('payment_method', '')
                total_amount = delivery_info.get('total_amount', 0)

                if payment_method == 'cash' and total_amount > 0:
                    # Cash payment - confirm collection amount
                    keyboard = DeliveryKeyboards.cash_collection_confirm(
                        language, delivery_id, total_amount
                    )
                    await query.edit_message_text(
                        i18n.get('staff.delivery.cash_collection', language,
                                 amount=format_currency(total_amount, language=language)),
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                    return

            # Standard confirmation for other statuses
            status_text = format_delivery_status(new_status, language)
            text = i18n.get('staff.delivery.confirm_status', language, status=status_text)

            keyboard = CommonKeyboards.confirm_cancel(
                language,
                confirm_data=f"staff_execute_status_{delivery_id}_{new_status}",
                cancel_data=f"staff_view_active_{delivery_id}"
            )

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error initiating status change: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def execute_status_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute the confirmed status change"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_execute_status_{delivery_id}_{new_status}
            parts = query.data.split('_')
            delivery_id = int(parts[3])
            new_status = '_'.join(parts[4:])

            metadata = context.user_data.pop('status_metadata', {})

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, new_status, metadata
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            # Success message
            status_text = format_delivery_status(new_status, language)
            success_msg = i18n.get('staff.delivery.status_updated', language, status=status_text)

            # Update current_delivery context
            if context.user_data.get('current_delivery'):
                context.user_data['current_delivery']['status'] = new_status

            if new_status in ('delivered', 'failed'):
                # Terminal status - go back to active deliveries
                keyboard = CommonKeyboards.back_button(language, "staff_active_deliveries")
            else:
                # Non-terminal - show delivery actions again
                keyboard = DeliveryKeyboards.active_delivery_actions(
                    language, delivery_id, new_status
                )

            await query.edit_message_text(
                f"\u2705 {success_msg}",
                reply_markup=keyboard,
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error executing status change: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def select_fail_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle failed delivery reason selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_failed_reason_{delivery_id}_{reason}
            parts = query.data.split('_')
            delivery_id = int(parts[3])
            reason = '_'.join(parts[4:])

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, 'failed',
                    metadata={'fail_reason': reason}
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            reason_text = i18n.get(f'staff.delivery.reason.{reason}', language)
            await query.edit_message_text(
                f"\u274c {i18n.get('staff.delivery.marked_failed', language)}\n"
                f"{i18n.get('staff.delivery.fail_reason_label', language)}: {reason_text}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error selecting fail reason: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm cash collection with the order total amount"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_confirm_cash_{delivery_id}
            delivery_id = int(query.data.split('_')[-1])
            delivery_info = context.user_data.get('current_delivery', {})
            cash_amount = delivery_info.get('total_amount', 0)

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, 'delivered',
                    metadata={'cash_collected': cash_amount}
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.delivery.delivered_success', language)}\n"
                f"\U0001f4b5 {i18n.get('staff.delivery.cash_recorded', language, amount=format_currency(cash_amount, language=language))}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error confirming cash collection: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def edit_cash_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt user to enter custom cash amount"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_id = int(query.data.split('_')[-1])
            context.user_data['editing_cash_delivery_id'] = delivery_id

            await query.edit_message_text(
                i18n.get('staff.delivery.enter_cash_amount', language),
                parse_mode='HTML'
            )

            return CASH_INPUT

        except Exception as e:
            logger.error(f"Error editing cash amount: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_cash_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and process custom cash amount"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            text = update.message.text.strip().replace(',', '').replace(' ', '')
            try:
                cash_amount = float(text)
                if cash_amount < 0:
                    raise ValueError("Negative amount")
            except ValueError:
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return CASH_INPUT

            delivery_id = context.user_data.pop('editing_cash_delivery_id', None)
            if not delivery_id:
                await update.message.reply_text(
                    i18n.get('staff.error_occurred', language)
                )
                return ConversationHandler.END

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, 'delivered',
                    metadata={'cash_collected': cash_amount}
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            await update.message.reply_text(
                f"\u2705 {i18n.get('staff.delivery.delivered_success', language)}\n"
                f"\U0001f4b5 {i18n.get('staff.delivery.cash_recorded', language, amount=format_currency(cash_amount, language=language))}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error receiving cash amount: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    async def mark_preparing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mark an order as preparing"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_mark_preparing_{order_id}
            order_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.mark_order_preparing(token, order_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.delivery.marked_preparing', language)}",
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error marking as preparing: {e}", exc_info=True)
            await self._handle_error(update, context)
