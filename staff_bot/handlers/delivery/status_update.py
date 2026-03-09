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
from utils.formatters import format_delivery_status, format_currency, get_cod_cash_projection
from permissions import require_auth, require_delivery_driver
from i18n import i18n
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS

logger = logging.getLogger(__name__)

# Conversation states handled via the global text router in staff_bot.bot.
CASH_INPUT = 100
CASH_NOTE_INPUT = 101
RECONCILIATION_INPUT = 102


class StatusUpdateHandler(BaseHandler):
    """Handle delivery status transitions"""

    @staticmethod
    def _parse_amount(raw_text: str) -> float:
        text = raw_text.strip().replace(',', '').replace(' ', '')
        cash_amount = float(text)
        if cash_amount < 0:
            raise ValueError("Negative amount")
        return cash_amount

    @staticmethod
    def _clear_delivery_cash_flow(context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop('pending_delivery_cash_flow', None)
        context.user_data.pop('pending_reconciliation_flow', None)

    @staticmethod
    def _get_expected_cash_to_collect(delivery_info: dict) -> float:
        return get_cod_cash_projection(delivery_info)['expected_cash_to_collect']

    @staticmethod
    def _format_session_summary(session: dict, language: str) -> str:
        status = session.get('status') or i18n.get('staff.common.not_available', language)
        expected_cash = format_currency(session.get('expected_cash'), language=language)
        expected_on_hand = format_currency(session.get('expected_cash_on_hand'), language=language)
        transferred_cash_total = format_currency(session.get('transferred_cash_total'), language=language)
        declared_cash = session.get('declared_cash')
        declared_variance = format_currency(session.get('declared_variance'), language=language)
        lines = [
            f"\U0001f9fe <b>{i18n.get('staff.menu.cash_reconciliation', language)}</b>",
            f"{i18n.get('staff.delivery.current_status', language)}: {status}",
            f"\U0001f4b0 {i18n.get('staff.delivery.expected_cash_label', language)}: {expected_cash}",
            f"\U0001f4e5 {i18n.get('staff.delivery.transferred_cash_label', language)}: {transferred_cash_total}",
            f"\U0001f45b {i18n.get('staff.delivery.expected_cash_on_hand_label', language)}: {expected_on_hand}",
        ]
        if declared_cash is not None:
            lines.append(
                f"\U0001f4b5 {i18n.get('staff.delivery.declared_cash_label', language)}: "
                f"{format_currency(declared_cash, language=language)}"
            )
            lines.append(
                f"\u26a0\ufe0f {i18n.get('staff.delivery.cash_variance_label', language)}: {declared_variance}"
            )
        notes = session.get('notes')
        if notes:
            lines.append(f"\U0001f4ac {notes}")
        risk_flags = session.get('risk_flags') or []
        if risk_flags:
            lines.append(
                f"\u26a0\ufe0f {i18n.get('staff.delivery.risk_flags', language)}: "
                f"{', '.join(str(flag) for flag in risk_flags)}"
            )
        return '\n'.join(lines)

    async def _submit_delivery_completion(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        delivery_id: int,
        cash_amount: float,
        notes: str = None,
    ):
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        metadata = {'cash_collected': cash_amount}
        if notes:
            metadata['notes'] = notes
        elif cash_amount <= 0:
            metadata['notes'] = 'No cash due after COD prepaid deduction'

        async with api_client as client:
            response = await client.update_delivery_status(
                token, delivery_id, 'delivered', metadata=metadata
            )

        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return ConversationHandler.END

        self._clear_delivery_cash_flow(context)
        if context.user_data.get('current_delivery'):
            context.user_data['current_delivery']['status'] = 'delivered'

        message = (
            f"\u2705 {i18n.get('staff.delivery.delivered_success', language)}\n"
            f"\U0001f4b5 {i18n.get('staff.delivery.cash_recorded', language, amount=format_currency(cash_amount, language=language))}"
        )
        if notes:
            message += f"\n\U0001f4ac {notes}"

        if update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML',
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML',
            )
        return ConversationHandler.END

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
                cash_due_amount = self._get_expected_cash_to_collect(delivery_info)
                reserved_prepayment = float(delivery_info.get('cod_reserved_prepayment_amount') or 0)

                if payment_method == 'cash' and cash_due_amount > 0:
                    keyboard = DeliveryKeyboards.cash_collection_options(
                        language, delivery_id, cash_due_amount
                    )
                    message_text = i18n.get(
                        'staff.delivery.cash_collection',
                        language,
                        amount=format_currency(cash_due_amount, language=language),
                    )
                    if reserved_prepayment > 0:
                        message_text += (
                            f"\n\U0001f4b3 COD prepaid deduction: "
                            f"{format_currency(reserved_prepayment, language=language)}"
                        )
                    await query.edit_message_text(
                        message_text,
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
    async def confirm_full_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete delivery and record the full cash amount."""
        query = update.callback_query
        await query.answer()

        try:
            delivery_id = int(query.data.split('_')[-1])
            delivery_info = context.user_data.get('current_delivery', {})
            cash_amount = self._get_expected_cash_to_collect(delivery_info)
            await self._submit_delivery_completion(
                update,
                context,
                delivery_id=delivery_id,
                cash_amount=cash_amount,
            )
        except Exception as e:
            logger.error(f"Error confirming cash collection: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_partial_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for a partial cash amount before delivery completion."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_id = int(query.data.split('_')[-1])
            context.user_data['pending_delivery_cash_flow'] = {
                'delivery_id': delivery_id,
                'flow_type': 'partial',
            }

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
    async def start_no_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for the required note when no cash was collected."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_id = int(query.data.split('_')[-1])
            context.user_data['pending_delivery_cash_flow'] = {
                'delivery_id': delivery_id,
                'flow_type': 'none',
                'cash_amount': 0.0,
            }
            await query.edit_message_text(
                i18n.get('staff.delivery.enter_no_cash_reason', language),
                parse_mode='HTML',
            )
            return CASH_NOTE_INPUT

        except Exception as e:
            logger.error(f"Error starting zero-cash delivery flow: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_cash_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive a partial cash amount and then require an audit note."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        if flow.get('flow_type') != 'partial':
            return ConversationHandler.END

        try:
            try:
                cash_amount = self._parse_amount(update.message.text)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return CASH_INPUT

            delivery_info = context.user_data.get('current_delivery', {})
            cash_due_amount = self._get_expected_cash_to_collect(delivery_info)
            if cash_amount <= 0 or (cash_due_amount and cash_amount > cash_due_amount):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return CASH_INPUT

            flow['cash_amount'] = cash_amount
            context.user_data['pending_delivery_cash_flow'] = flow
            await update.message.reply_text(
                i18n.get('staff.delivery.enter_partial_cash_reason', language),
                parse_mode='HTML',
            )
            return CASH_NOTE_INPUT

        except Exception as e:
            logger.error(f"Error receiving cash amount: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def receive_cash_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive the required note for a partial or zero-cash delivery completion."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        if flow.get('flow_type') not in {'partial', 'none'}:
            return ConversationHandler.END

        try:
            note = (update.message.text or '').strip()
            if not note:
                await update.message.reply_text(
                    i18n.get('staff.delivery.note_required', language)
                )
                return CASH_NOTE_INPUT

            delivery_id = flow.get('delivery_id')
            cash_amount = flow.get('cash_amount', 0.0)
            return await self._submit_delivery_completion(
                update,
                context,
                delivery_id=delivery_id,
                cash_amount=cash_amount,
                notes=note,
            )

        except Exception as e:
            logger.error(f"Error receiving cash collection note: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def show_reconciliation_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the driver's open reconciliation session."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_reconciliation_session(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            session = response.data or {}
            text = self._format_session_summary(session, language)
            keyboard = DeliveryKeyboards.reconciliation_actions(
                language,
                can_submit=session.get('status') in {'open', 'overdue'},
                can_handoff=session.get('status') in {'open', 'submitted', 'overdue', 'mismatch'},
            )

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing reconciliation session: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_reconciliation_submit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to enter the counted cash for reconciliation."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        context.user_data['pending_reconciliation_flow'] = {'action': 'submit'}
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_declared_cash', language),
            parse_mode='HTML',
        )
        return RECONCILIATION_INPUT

    @require_auth
    @require_delivery_driver
    async def start_reconciliation_transfer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to enter checkpoint handoff amount."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        context.user_data['pending_reconciliation_flow'] = {'action': 'transfer'}
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_transfer_cash', language),
            parse_mode='HTML',
        )
        return RECONCILIATION_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_reconciliation_declared_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Submit the declared driver cash amount for reconciliation."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        if not context.user_data.get('pending_reconciliation_flow'):
            return ConversationHandler.END

        try:
            try:
                declared_cash = self._parse_amount(update.message.text)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return RECONCILIATION_INPUT

            flow = context.user_data.get('pending_reconciliation_flow') or {}
            flow_action = flow.get('action', 'submit')
            async with api_client as client:
                if flow_action == 'transfer':
                    response = await client.create_reconciliation_transfer(
                        token,
                        {'declared_transfer_cash': declared_cash},
                    )
                else:
                    response = await client.submit_reconciliation_session(
                        token,
                        {'declared_cash': declared_cash},
                    )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            self._clear_delivery_cash_flow(context)
            session = response.data or {}
            if flow_action == 'transfer':
                success_title = i18n.get('staff.delivery.transfer_created', language)
            else:
                success_title = i18n.get('staff.delivery.reconciliation_submitted', language)
            message = f"\u2705 {success_title}\n\n{self._format_session_summary(session, language)}"
            await update.message.reply_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_back_to_main"),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error submitting reconciliation session: {e}", exc_info=True)
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
