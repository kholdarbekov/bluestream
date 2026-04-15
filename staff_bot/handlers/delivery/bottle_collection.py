"""Standalone bottle collection and fine creation flow for delivery drivers."""

import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils.formatters import escape_html, format_currency, format_user_card
from staff_bot.utils.search import detect_search_type

logger = logging.getLogger(__name__)

BOTTLE_COLLECTION_SEARCH_INPUT = 107
BOTTLE_COLLECTION_QTY_INPUT = 108
BOTTLE_COLLECTION_NOTE_INPUT = 109
BOTTLE_FINE_QTY_INPUT = 110
BOTTLE_FINE_AMOUNT_INPUT = 111
BOTTLE_FINE_NOTE_INPUT = 112
BOTTLES_LOADED_INPUT = 113
BOTTLES_RETURNED_WH_INPUT = 114
BOTTLE_SESSION_LOADED_QTY_INPUT = 120
BOTTLE_SESSION_RETURNED_QTY_INPUT = 121
BOTTLE_TRANSFER_DRIVER_SELECT = 122
BOTTLE_TRANSFER_QTY_INPUT = 123
BOTTLE_TRANSFER_CONFIRM_QTY_INPUT = 124


class BottleCollectionHandler(BaseHandler):
    """Handle standalone bottle collection and fine creation outside delivery flow."""

    @staticmethod
    def _clear_flow(context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop('pending_bottle_collection_flow', None)

    @staticmethod
    def _format_bottle_statement(summary: dict, language: str) -> str:
        addresses = summary.get('addresses') or []
        total = summary.get('total_balance', 0)
        fines = summary.get('active_fines_count', 0)
        fine_amount = summary.get('total_fine_amount', 0)

        lines = [
            f"\U0001f4ca <b>{i18n.get('staff.delivery.bottle_statement_title', language)}</b>",
            f"\U0001f4e6 {i18n.get('staff.delivery.total_bottles', language)}: {int(total)}",
        ]
        if fines > 0:
            lines.append(
                f"\u26a0\ufe0f {i18n.get('staff.delivery.active_fines', language)}: {fines} "
                f"({format_currency(fine_amount, language=language)})"
            )

        if not addresses:
            lines.append(i18n.get('staff.delivery.no_bottle_balance', language))
            return '\n'.join(lines)

        lines.append('')
        for addr in addresses:
            balance = addr.get('balance', 0)
            if balance <= 0:
                continue
            title = addr.get('address_title') or addr.get('full_address', '')[:30]
            lines.append(f"\u2022 {escape_html(title)}: {int(balance)}")

        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Standalone bottle collection flow
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to search for a customer with bottles."""
        language = await self._get_language(update, context)
        self._clear_flow(context)

        text = i18n.get('staff.delivery.bottle_collection_search_prompt', language)
        cancel_keyboard = CommonKeyboards.back_button(language, "staff_cash_hub")
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=cancel_keyboard, parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=cancel_keyboard, parse_mode='HTML'
            )
        return BOTTLE_COLLECTION_SEARCH_INPUT

    async def receive_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search customers with bottle balance > 0."""
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
            return BOTTLE_COLLECTION_SEARCH_INPUT

        try:
            search_type = detect_search_type(query_text)
            async with api_client as client:
                response = await client.search_customers(
                    token, query_text,
                    search_type=search_type,
                    only_with_bottles=True,
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return BOTTLE_COLLECTION_SEARCH_INPUT

            customers = response.data if isinstance(response.data, list) else response.data.get('items', [])
            if not customers:
                await update.message.reply_text(
                    i18n.get('staff.delivery.no_customer_bottle_results', language, query=escape_html(query_text)),
                    reply_markup=CommonKeyboards.back_button(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            for customer in customers[:10]:
                card = format_user_card(customer, language)
                await update.message.reply_text(
                    card,
                    reply_markup=DeliveryKeyboards.bottle_customer_result(language, customer['id']),
                    parse_mode='HTML',
                )

            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error searching customers for bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def show_customer_bottle_statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bottle balance for selected customer + their addresses."""
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
                response = await client.get_customer_bottle_summary(token, customer_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            summary = response.data or {}
            context.user_data['pending_bottle_collection_flow'] = {
                'customer_id': customer_id,
            }

            text = self._format_bottle_statement(summary, language)
            addresses = [a for a in (summary.get('addresses') or []) if a.get('balance', 0) > 0]

            if addresses:
                keyboard = DeliveryKeyboards.bottle_address_selection(
                    language, customer_id, addresses
                )
            else:
                keyboard = CommonKeyboards.back_button(language)

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        except Exception as exc:
            logger.error("Error showing customer bottle statement: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def select_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Driver selects which address they're collecting from."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_bottle_addr_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            flow = context.user_data.get('pending_bottle_collection_flow') or {}
            flow['customer_id'] = customer_id
            flow['address_id'] = address_id
            context.user_data['pending_bottle_collection_flow'] = flow

            keyboard = DeliveryKeyboards.bottle_statement_actions(
                language, customer_id, address_id
            )
            await query.edit_message_text(
                i18n.get('staff.delivery.bottle_address_selected', language),
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error selecting bottle address: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for bottle quantity to collect."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_bottle_collect_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            flow = context.user_data.get('pending_bottle_collection_flow') or {}
            flow['customer_id'] = customer_id
            flow['address_id'] = address_id
            flow['action'] = 'collect'
            context.user_data['pending_bottle_collection_flow'] = flow

            await query.edit_message_text(
                i18n.get('staff.delivery.enter_bottle_collection_qty', language),
                parse_mode='HTML',
            )
            return BOTTLE_COLLECTION_QTY_INPUT
        except Exception as exc:
            logger.error("Error starting bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    async def receive_collection_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive bottle count, ask for notes."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'collect':
            return ConversationHandler.END

        try:
            count = int(update.message.text.strip())
            if count <= 0:
                raise ValueError("non-positive")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_COLLECTION_QTY_INPUT

        flow['quantity'] = count
        context.user_data['pending_bottle_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_bottle_collection_note', language),
            parse_mode='HTML',
        )
        return BOTTLE_COLLECTION_NOTE_INPUT

    async def receive_collection_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finalize standalone bottle collection."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        customer_id = flow.get('customer_id')
        address_id = flow.get('address_id')
        quantity = flow.get('quantity')
        notes = update.message.text.strip()

        if not all([customer_id, address_id, quantity]):
            await update.message.reply_text(i18n.get('staff.error_occurred', language))
            self._clear_flow(context)
            return ConversationHandler.END

        try:
            async with api_client as client:
                response = await client.record_bottle_collection(
                    token,
                    {
                        'customer_id': customer_id,
                        'address_id': address_id,
                        'quantity': quantity,
                        'notes': notes,
                    },
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            result = response.data or {}
            self._clear_flow(context)
            await update.message.reply_text(
                i18n.get(
                    'staff.delivery.bottle_collection_recorded', language,
                    quantity=quantity,
                    remaining=int(result.get('remaining_balance', 0)),
                ),
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error recording bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Manual fine creation flow
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_fine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start fine creation from customer statement."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_bottle_fine_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            flow = context.user_data.get('pending_bottle_collection_flow') or {}
            flow['customer_id'] = customer_id
            flow['address_id'] = address_id
            flow['action'] = 'fine'
            context.user_data['pending_bottle_collection_flow'] = flow

            await query.edit_message_text(
                i18n.get('staff.delivery.enter_fine_bottle_qty', language),
                parse_mode='HTML',
            )
            return BOTTLE_FINE_QTY_INPUT
        except Exception as exc:
            logger.error("Error starting bottle fine: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    async def receive_fine_bottle_qty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive how many bottles to fine for."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'fine':
            return ConversationHandler.END

        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_FINE_QTY_INPUT

        flow['fine_quantity'] = qty
        context.user_data['pending_bottle_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_fine_amount', language),
            parse_mode='HTML',
        )
        return BOTTLE_FINE_AMOUNT_INPUT

    async def receive_fine_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive monetary fine amount."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'fine':
            return ConversationHandler.END

        try:
            amount = float(update.message.text.strip().replace(',', '').replace(' ', ''))
            if amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_amount', language)
            )
            return BOTTLE_FINE_AMOUNT_INPUT

        flow['fine_amount'] = amount
        context.user_data['pending_bottle_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_fine_note', language),
            parse_mode='HTML',
        )
        return BOTTLE_FINE_NOTE_INPUT

    async def receive_fine_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Submit fine creation."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        notes = update.message.text.strip()

        try:
            # We need the bottle_balance_id. Fetch it from the customer summary.
            customer_id = flow.get('customer_id')
            address_id = flow.get('address_id')

            async with api_client as client:
                summary_response = await client.get_customer_bottle_summary(token, customer_id)

            if not summary_response.success:
                await self._handle_api_response_error(update, summary_response, language)
                return ConversationHandler.END

            # Find bottle_balance_id for this address
            addresses = (summary_response.data or {}).get('addresses', [])
            bottle_balance_id = None
            for addr in addresses:
                if addr.get('address_id') == address_id:
                    bottle_balance_id = addr.get('bottle_balance_id')
                    break

            if not bottle_balance_id:
                await update.message.reply_text(i18n.get('staff.error_occurred', language))
                self._clear_flow(context)
                return ConversationHandler.END

            async with api_client as client:
                response = await client.create_bottle_fine(
                    token,
                    {
                        'customer_id': customer_id,
                        'bottle_balance_id': bottle_balance_id,
                        'quantity': flow.get('fine_quantity'),
                        'fine_amount': flow.get('fine_amount'),
                        'notes': notes,
                    },
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            self._clear_flow(context)
            await update.message.reply_text(
                i18n.get(
                    'staff.delivery.bottle_fine_created', language,
                    quantity=flow.get('fine_quantity'),
                    amount=format_currency(flow.get('fine_amount'), language=language),
                ),
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error creating bottle fine: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Session formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_session(session: dict, language: str) -> str:
        """Format a DriverBottleSession as an HTML summary block."""
        status = session.get('status', 'open')
        loaded = session.get('bottles_loaded', 0)
        delivered = session.get('bottles_delivered', 0)
        collected = session.get('bottles_collected_from_customers', 0)
        transferred_out = session.get('bottles_transferred_out', 0)
        transferred_in = session.get('bottles_transferred_in', 0)
        current = session.get('current_inventory', 0)
        returned = session.get('bottles_returned_to_warehouse')
        discrepancy = session.get('discrepancy')
        started_at = session.get('started_at', '')[:16].replace('T', ' ')
        ref = (session.get('session_ref') or '')[:8]

        session_label = i18n.get('staff.delivery.session_ref_label', language)
        started_label = i18n.get('staff.delivery.session_started_label', language)
        loaded_label = i18n.get('staff.delivery.bottles_loaded_label', language)
        delivered_label = i18n.get('staff.delivery.bottles_delivered_label', language)
        collected_label = i18n.get('staff.delivery.bottles_collected_label', language)
        transferred_out_label = i18n.get('staff.delivery.bottles_transferred_out_label', language)
        transferred_in_label = i18n.get('staff.delivery.bottles_transferred_in_label', language)
        on_truck_label = i18n.get('staff.delivery.bottles_on_truck_label', language)
        returned_wh_label = i18n.get('staff.delivery.bottles_returned_wh_label', language)
        discrepancy_label = i18n.get('staff.delivery.discrepancy_label', language)

        lines = [
            f"\U0001f69a <b>{session_label} #{escape_html(ref)}</b>  [{escape_html(status.upper())}]",
            f"\u23f1 {started_label}: {escape_html(started_at)}",
            "",
            f"\U0001f4e6 {loaded_label}:               <b>{loaded}</b>",
            f"\U0001f69a {delivered_label}:            <b>{delivered}</b>",
            f"\u267b\ufe0f {collected_label}:            <b>{collected}</b>",
        ]
        if transferred_out or transferred_in:
            lines += [
                f"\U0001f4e4 {transferred_out_label}:      <b>{transferred_out}</b>",
                f"\U0001f4e5 {transferred_in_label}:       <b>{transferred_in}</b>",
            ]
        lines.append("\u2500" * 30)
        lines.append(f"\U0001f69a {on_truck_label}:         <b>{current}</b>")
        if returned is not None:
            lines.append(f"\U0001f3e2 {returned_wh_label}:       <b>{returned}</b>")
        if discrepancy is not None:
            if discrepancy == 0:
                lines.append(i18n.get('staff.delivery.discrepancy_zero', language))
            else:
                lines.append(
                    f"\u26a0\ufe0f {discrepancy_label}:          <b>{discrepancy}</b>"
                )
        return "\n".join(lines)

    @staticmethod
    def _format_accountability(record: dict, language: str) -> str:
        """Format a legacy DriverBottleLoad record as HTML summary (kept for backward compat)."""
        load_date = record.get('load_date', '')
        loaded = record.get('bottles_loaded', 0)
        delivered = record.get('bottles_delivered', 0)
        collected = record.get('bottles_collected', 0)
        returned = record.get('bottles_returned_to_warehouse', 0)
        discrepancy = record.get('discrepancy', 0)

        lines = [
            f"\U0001f4ca <b>Bottle Accountability</b>"
        ]
        if load_date:
            lines[0] += f" ({escape_html(str(load_date))})"
        lines += [
            f"\U0001f4e6 Loaded:          <b>{loaded}</b>",
            f"\U0001f69a Delivered:       <b>{delivered}</b>",
            f"\u267b\ufe0f  Collected:       <b>{collected}</b>",
            f"\U0001f3e2 Returned to WH:  <b>{returned}</b>",
            "\u2500" * 28,
        ]
        if discrepancy == 0:
            lines.append(f"\u2705 Discrepancy:     <b>0</b>")
        else:
            lines.append(f"\u26a0\ufe0f Discrepancy:     <b>{discrepancy}</b>")
        return "\n".join(lines)

    @require_auth
    @require_delivery_driver
    async def show_my_accountability(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show driver's current session or most recent closed session."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            session = response.data
            if session:
                text = self._format_session(session, language)
            else:
                text = i18n.get('staff.delivery.bottle_accountability_no_data', language)

            await query.edit_message_text(
                text,
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error showing bottle session: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    # ------------------------------------------------------------------
    # Session: Open (Load from Warehouse)
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_log_loaded(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start opening a new session. Block if an open session already exists."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # Check for existing open session
        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)
            if response.success and response.data:
                session = response.data
                raw_started = session.get('started_at')
                started = raw_started[:16].replace('T', ' ') if raw_started else 'unknown time'
                loaded = session.get('bottles_loaded', 0)
                text = i18n.get(
                    'staff.delivery.bottle_session_already_open', language,
                    started=escape_html(started), loaded=loaded,
                )
                await query.edit_message_text(
                    text,
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END
        except Exception:
            pass

        prompt = i18n.get('staff.delivery.enter_bottles_loaded_qty', language)
        await query.edit_message_text(
            prompt,
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )
        return BOTTLE_SESSION_LOADED_QTY_INPUT

    async def receive_bottles_loaded(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open a new session with the entered bottle count."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            count = int(update.message.text.strip())
            if count <= 0:
                raise ValueError("non-positive")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_SESSION_LOADED_QTY_INPUT

        try:
            async with api_client as client:
                response = await client.open_bottle_session(token, count)

            if not response.success:
                # Check for already-open session error
                error_code = (response.data or {}).get('error_code', '')
                if error_code == 'BOTTLE_SESSION_ALREADY_OPEN':
                    await update.message.reply_text(
                        i18n.get('staff.delivery.bottle_session_already_open_short', language),
                        reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                        parse_mode='HTML',
                    )
                    return ConversationHandler.END
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            session = response.data or {}
            ref = (session.get('session_ref') or '')[:8]
            text = i18n.get(
                'staff.delivery.bottle_session_opened', language,
                count=count, ref=escape_html(ref),
            )
            await update.message.reply_text(
                text,
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error opening bottle session: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Session: Close (Return to Warehouse)
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_return_to_warehouse(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show session summary and prompt driver for return count."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # Fetch open session for context display
        context_text = ""
        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)
            if response.success and response.data:
                context_text = self._format_session(response.data, language) + "\n\n"
            elif response.success and not response.data:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_active_bottle_session', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END
        except Exception:
            pass

        prompt = i18n.get('staff.delivery.enter_bottles_returned_qty', language)
        await query.edit_message_text(
            context_text + prompt,
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )
        return BOTTLE_SESSION_RETURNED_QTY_INPUT

    async def receive_bottles_returned(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Close the active session with the returned bottle count."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            count = int(update.message.text.strip())
            if count < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_SESSION_RETURNED_QTY_INPUT

        try:
            async with api_client as client:
                response = await client.close_bottle_session(token, count)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            session = response.data or {}
            discrepancy = session.get('discrepancy', 0)
            ref = (session.get('session_ref') or '')[:8]

            disc_line = (
                i18n.get('staff.delivery.discrepancy_zero', language)
                if discrepancy == 0
                else i18n.get('staff.delivery.discrepancy_nonzero', language, discrepancy=discrepancy)
            )
            text = i18n.get(
                'staff.delivery.bottle_session_closed', language,
                count=count, disc_line=disc_line, ref=escape_html(ref),
            )
            await update.message.reply_text(
                text,
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error closing bottle session: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Transfer: Sender side
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_transfer_bottles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start transfer flow: check open session, then show active driver list."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # Must have open session
        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)
            if not (response.success and response.data):
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_active_bottle_session', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            session = response.data
            available = session.get('current_inventory', 0)
            if available <= 0:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_bottles_to_transfer', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            context.user_data['pending_transfer_available'] = available
        except Exception as exc:
            logger.error("Error checking session for transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

        # Fetch active drivers list
        try:
            async with api_client as client:
                drivers_response = await client.get_active_drivers(token)

            if drivers_response.success and drivers_response.data:
                drivers = drivers_response.data
            else:
                drivers = []
        except Exception:
            drivers = []

        if not drivers:
            await query.edit_message_text(
                i18n.get('staff.delivery.no_active_drivers', language),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        await query.edit_message_text(
            i18n.get('staff.delivery.select_transfer_driver', language, available=available),
            reply_markup=DeliveryKeyboards.driver_select_for_transfer(language, drivers),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_DRIVER_SELECT

    async def receive_transfer_driver_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store selected receiver driver and prompt for quantity."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        data = query.data  # e.g. "staff_transfer_driver_42"
        try:
            receiver_id = int(data.split('_')[-1])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return ConversationHandler.END

        available = context.user_data.get('pending_transfer_available', 0)
        context.user_data['pending_transfer_receiver_id'] = receiver_id

        await query.edit_message_text(
            i18n.get('staff.delivery.enter_transfer_qty', language, available=available),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_QTY_INPUT

    async def receive_transfer_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send transfer and notify result."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        receiver_id = context.user_data.get('pending_transfer_receiver_id')
        available = context.user_data.get('pending_transfer_available', 0)

        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                raise ValueError("non-positive")
            if qty > available:
                await update.message.reply_text(
                    i18n.get('staff.delivery.transfer_qty_exceeds_available', language, available=available)
                )
                return BOTTLE_TRANSFER_QTY_INPUT
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_TRANSFER_QTY_INPUT

        try:
            async with api_client as client:
                response = await client.initiate_bottle_transfer(token, receiver_id, qty)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            transfer = response.data or {}
            ref = (transfer.get('transfer_ref') or '')[:8]
            await update.message.reply_text(
                i18n.get(
                    'staff.delivery.bottle_transfer_initiated', language,
                    qty=qty, ref=escape_html(ref),
                ),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            context.user_data.pop('pending_transfer_receiver_id', None)
            context.user_data.pop('pending_transfer_available', None)
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error initiating bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Transfer: Receiver confirmation
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def show_pending_transfers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show pending incoming transfers for the driver."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_pending_bottle_transfers(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            transfers = response.data or []
            if not transfers:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_pending_transfers', language),
                    reply_markup=CommonKeyboards.back_button(language, callback_data="staff_cash_hub"),
                    parse_mode='HTML',
                )
                return

            lines = [i18n.get('staff.delivery.pending_transfers_title', language) + "\n"]
            for t in transfers:
                ref = (t.get('transfer_ref') or '')[:8]
                qty = t.get('declared_quantity', 0)
                sender = t.get('sender_name', 'Unknown driver')
                lines.append(f"• From <b>{escape_html(sender)}</b>: <b>{qty}</b> bottles  [ref: {escape_html(ref)}]")

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=DeliveryKeyboards.pending_transfer_list(language, transfers),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error fetching pending transfers: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_transfer_custom_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point: receiver taps 'Different count' — prompt for actual qty."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        # callback_data: staff_transfer_custom_<transfer_id>
        data = query.data
        try:
            transfer_id = int(data.split('_')[-1])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return ConversationHandler.END

        context.user_data['pending_confirm_transfer_id'] = transfer_id
        language = await self._get_language(update, context)
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_actual_received_qty', language),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_CONFIRM_QTY_INPUT

    async def receive_transfer_confirm_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback when receiver taps 'Confirm N' on a transfer."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        # callback_data: staff_transfer_confirm_<transfer_id>_<qty>
        data = query.data
        parts = data.split('_')
        try:
            transfer_id = int(parts[-2])
            qty = int(parts[-1])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return

        context.user_data['pending_confirm_transfer_id'] = transfer_id
        context.user_data['pending_confirm_transfer_qty'] = qty
        # Direct confirm with declared qty
        await self._do_confirm_transfer(update, context, transfer_id, qty, language)

    async def receive_transfer_custom_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive a custom quantity from the receiver and confirm the transfer."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        transfer_id = context.user_data.get('pending_confirm_transfer_id')
        if not transfer_id:
            return ConversationHandler.END

        try:
            qty = int(update.message.text.strip())
            if qty < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_TRANSFER_CONFIRM_QTY_INPUT

        await self._do_confirm_transfer(update, context, transfer_id, qty, language)
        return ConversationHandler.END

    async def _do_confirm_transfer(self, update, context, transfer_id: int, qty: int, language: str):
        """API call to confirm/dispute a transfer and show result."""
        token = await self._get_auth_token(update, context)
        if not token:
            return

        try:
            async with api_client as client:
                response = await client.confirm_bottle_transfer(token, transfer_id, qty)

            if not response.success:
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        i18n.get('staff.delivery.transfer_confirm_failed', language),
                        reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    )
                else:
                    await update.message.reply_text(
                        i18n.get('staff.delivery.transfer_confirm_failed', language)
                    )
                return

            transfer = response.data or {}
            status = transfer.get('status', 'confirmed')
            declared = transfer.get('declared_quantity', 0)

            if status == 'confirmed':
                text = i18n.get('staff.delivery.transfer_confirmed', language, qty=qty)
            else:
                text = i18n.get(
                    'staff.delivery.transfer_disputed', language,
                    declared=declared, qty=qty,
                )

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text,
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
            else:
                await update.message.reply_text(
                    text,
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
            context.user_data.pop('pending_confirm_transfer_id', None)
            context.user_data.pop('pending_confirm_transfer_qty', None)
        except Exception as exc:
            logger.error("Error confirming bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
