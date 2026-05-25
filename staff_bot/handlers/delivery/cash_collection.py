"""Standalone COD debt collection flow for delivery drivers."""

import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import escape_html, format_currency, format_user_card
from staff_bot.utils.search import detect_search_type

logger = logging.getLogger(__name__)

COLLECTION_SEARCH_INPUT = 103
COLLECTION_AMOUNT_INPUT = 104
COLLECTION_NOTE_INPUT = 105


class CashCollectionHandler(BaseHandler):
    """Handle standalone COD debt collection outside delivery completion."""

    @staticmethod
    async def _clear_flow(
        context: ContextTypes.DEFAULT_TYPE,
        update: Update = None,
    ):
        """Clear the standalone-COD flow flag plus the Redis mirror, and
        deliver any pool-insertion suggestions deferred while the driver
        was mid-collection. See `flow_state.clear_and_drain` for the queue
        protocol; `update` is optional so legacy call sites keep working
        with degraded (in-memory-only) behaviour."""
        context.user_data.pop('pending_cod_collection_flow', None)
        if update and update.effective_user:
            language = context.user_data.get('language') if context else None
            await flow_state.clear_and_drain(
                update.effective_user.id, context.bot, language=language
            )

    @staticmethod
    def _format_statement(statement: dict, language: str) -> str:
        items = statement.get('items') or []
        lines = [
            f"📜 <b>{i18n.get('staff.delivery.cod_statement_title', language)}</b>",
            f"💳 {i18n.get('staff.delivery.active_cod_debts', language)}: {statement.get('active_cod_debt_count', 0)}",
            f"💰 {i18n.get('staff.delivery.total_outstanding', language)}: {format_currency(statement.get('total_outstanding_amount', 0), language=language)}",
        ]

        if not items:
            lines.append(i18n.get('staff.delivery.no_cod_debt', language))
            return '\n'.join(lines)

        lines.append('')
        for item in items[:5]:
            if float(item.get('outstanding_amount') or 0) <= 0:
                continue
            lines.append(
                f"• {escape_html(item.get('order_number') or i18n.get('staff.order.unknown', language))}: "
                f"{format_currency(item.get('outstanding_amount') or 0, language=language)}"
            )
        return '\n'.join(lines)

    @require_auth
    @require_delivery_driver
    async def start_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to search for a customer with open COD debt."""
        language = await self._get_language(update, context)
        await self._clear_flow(context, update)

        text = i18n.get('staff.delivery.cod_collection_search_prompt', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text(text, parse_mode='HTML')
        return COLLECTION_SEARCH_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search customers with open COD debt for standalone collection."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        query_text = update.message.text.strip()
        if len(query_text) < 2:
            await update.message.reply_text(
                i18n.get('staff.operator.search_too_short', language),
                parse_mode='HTML',
            )
            return COLLECTION_SEARCH_INPUT

        try:
            search_type = detect_search_type(query_text)
            async with api_client as client:
                response = await client.search_customers(
                    token,
                    query_text,
                    search_type=search_type,
                    only_with_open_cod=True,
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return COLLECTION_SEARCH_INPUT

            customers = response.data if isinstance(response.data, list) else response.data.get('items', [])
            if not customers:
                await update.message.reply_text(
                    i18n.get('staff.delivery.no_customer_cod_results', language, query=escape_html(query_text)),
                    reply_markup=CommonKeyboards.back_button(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            for customer in customers[:10]:
                card = format_user_card(customer, language)
                card += (
                    f"\n💳 {i18n.get('staff.delivery.active_cod_debts', language)}: {customer.get('active_cod_debt_count', 0)}"
                    f"\n💰 {i18n.get('staff.delivery.total_outstanding', language)}: "
                    f"{format_currency(customer.get('total_outstanding_amount') or 0, language=language)}"
                )
                await update.message.reply_text(
                    card,
                    reply_markup=DeliveryKeyboards.cod_customer_result(language, customer['id']),
                    parse_mode='HTML',
                )

            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error searching customers for COD collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def show_customer_statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show COD debt statement for a selected customer."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            customer_id = int(query.data.split('_')[-1])
            async with api_client as client:
                response = await client.get_customer_cod_statement(token, customer_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            statement = response.data or {}
            context.user_data['pending_cod_collection_flow'] = {
                'customer_id': customer_id,
                'total_outstanding_amount': statement.get('total_outstanding_amount', 0),
            }

            await query.edit_message_text(
                self._format_statement(statement, language),
                reply_markup=DeliveryKeyboards.cod_statement_actions(
                    language,
                    customer_id,
                    can_collect=(statement.get('active_cod_debt_count', 0) > 0),
                ),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error showing customer COD statement: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_full_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prepare a full outstanding-balance standalone collection."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        customer_id = int(query.data.split('_')[-1])
        async with api_client as client:
            response = await client.get_customer_cod_statement(token, customer_id)

        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        statement = response.data or {}
        total_outstanding = float(statement.get('total_outstanding_amount') or 0)
        if total_outstanding <= 0:
            await query.answer(i18n.get('staff.delivery.no_cod_debt', language), show_alert=True)
            return

        context.user_data['pending_cod_collection_flow'] = {
            'customer_id': customer_id,
            'amount': total_outstanding,
            'total_outstanding_amount': total_outstanding,
        }
        # C-2: mirror the flow into Redis so the webhook server can defer
        # pool-insertion suggestions until this collection completes.
        await flow_state.mark_active(
            update.effective_user.id, 'pending_cod_collection_flow'
        )
        await query.edit_message_text(
            i18n.get(
                'staff.delivery.cod_collection_note_prompt',
                language,
                amount=format_currency(total_outstanding, language=language),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def start_custom_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to enter a custom standalone collection amount."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        customer_id = int(query.data.split('_')[-1])
        context.user_data['pending_cod_collection_flow'] = {
            'customer_id': customer_id,
        }
        await flow_state.mark_active(
            update.effective_user.id, 'pending_cod_collection_flow'
        )
        await query.edit_message_text(
            i18n.get('staff.delivery.cod_collection_amount_prompt', language),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_AMOUNT_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive custom amount for standalone collection."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        flow = context.user_data.get('pending_cod_collection_flow') or {}
        customer_id = flow.get('customer_id')
        if not customer_id:
            await update.message.reply_text(i18n.get('staff.error_occurred', language))
            return ConversationHandler.END

        try:
            amount = float(update.message.text.strip().replace(',', '').replace(' ', ''))
            if amount <= 0:
                raise ValueError("non-positive")
        except ValueError:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_cash_amount', language))
            return COLLECTION_AMOUNT_INPUT

        async with api_client as client:
            response = await client.get_customer_cod_statement(token, customer_id)

        if response.success:
            statement = response.data or {}
            total_outstanding = float(statement.get('total_outstanding_amount') or 0)
            if total_outstanding > 0 and amount > total_outstanding:
                await update.message.reply_text(
                    i18n.get(
                        'staff.delivery.cod_collection_amount_exceeds_outstanding',
                        language,
                        amount=format_currency(total_outstanding, language=language),
                    ),
                    parse_mode='HTML',
                )
                return COLLECTION_AMOUNT_INPUT
            flow['total_outstanding_amount'] = total_outstanding

        flow['amount'] = amount
        context.user_data['pending_cod_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get(
                'staff.delivery.cod_collection_note_prompt',
                language,
                amount=format_currency(amount, language=language),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finalize standalone COD collection after receiving notes."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        flow = context.user_data.get('pending_cod_collection_flow') or {}
        customer_id = flow.get('customer_id')
        amount = flow.get('amount')
        notes = update.message.text.strip()
        if not customer_id or amount is None:
            await update.message.reply_text(i18n.get('staff.error_occurred', language))
            await self._clear_flow(context, update)
            return ConversationHandler.END
        if not notes:
            await update.message.reply_text(i18n.get('staff.delivery.collection_notes_required', language))
            return COLLECTION_NOTE_INPUT

        try:
            async with api_client as client:
                response = await client.record_cash_collection(
                    token,
                    {
                        'customer_id': customer_id,
                        'amount': amount,
                        'source': 'standalone_meeting',
                        'notes': notes,
                        'proof_data': {'channel': 'staff_bot', 'flow': 'standalone_cod_collection'},
                    },
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            async with api_client as client:
                statement_response = await client.get_customer_cod_statement(token, customer_id)

            remaining_outstanding = 0
            if statement_response.success:
                remaining_outstanding = float((statement_response.data or {}).get('total_outstanding_amount') or 0)

            await self._clear_flow(context, update)
            await update.message.reply_text(
                i18n.get(
                    'staff.delivery.cod_collection_recorded',
                    language,
                    amount=format_currency(amount, language=language),
                    remaining=format_currency(remaining_outstanding, language=language),
                ),
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error recording standalone COD collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END
