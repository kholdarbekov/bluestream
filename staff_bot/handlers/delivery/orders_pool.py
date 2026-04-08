"""
Orders Pool Handler for Staff Bot
Shows unassigned orders available for pickup by delivery persons.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import format_order_card, format_currency, escape_html, get_cod_cash_projection
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)


class OrdersPoolHandler(BaseHandler):
    """Handle order pool browsing and acceptance"""

    @require_auth
    @require_delivery_driver
    async def show_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show available orders pool"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            page = context.user_data.get('pool_page', 1)
            async with api_client as client:
                response = await client.get_order_pool(token, filters={'page': page, 'per_page': 10})

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            orders = response.data.get('items', [])
            pagination = response.data.get('pagination', {})

            if not orders:
                text = f"\U0001f4e6 {i18n.get('staff.delivery.pool_empty', language)}"
                keyboard = CommonKeyboards.back_button(language)

                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text, reply_markup=keyboard, parse_mode='HTML'
                    )
                else:
                    await update.message.reply_text(
                        text, reply_markup=keyboard, parse_mode='HTML'
                    )
                return

            # Build pool message
            header = f"\U0001f4e6 <b>{i18n.get('staff.delivery.pool_title', language)}</b>"
            header += f"\n{i18n.get('staff.delivery.pool_count', language, count=pagination.get('total', len(orders)))}\n"

            if update.callback_query:
                await update.callback_query.edit_message_text(header, parse_mode='HTML')
            else:
                await update.message.reply_text(header, parse_mode='HTML')

            # Send each order as a separate message with action buttons
            for order in orders:
                card = format_order_card(order, language)
                delivery_id = order.get('delivery_id') or order.get('id')
                keyboard = DeliveryKeyboards.order_pool_item(language, delivery_id)

                if update.callback_query:
                    await update.callback_query.message.reply_text(
                        card, reply_markup=keyboard, parse_mode='HTML'
                    )
                else:
                    await update.message.reply_text(
                        card, reply_markup=keyboard, parse_mode='HTML'
                    )

            # Send pagination if needed
            total_pages = pagination.get('pages', 1)
            if total_pages > 1:
                page_buttons = CommonKeyboards.pagination(
                    language, page, total_pages, 'staff_pool'
                )
                from telegram import InlineKeyboardMarkup
                page_keyboard = InlineKeyboardMarkup([page_buttons])
                target = update.callback_query.message if update.callback_query else update.message
                await target.reply_text(
                    f"{i18n.get('staff.page', language)} {page}/{total_pages}",
                    reply_markup=page_keyboard
                )

        except Exception as e:
            logger.error(f"Error showing order pool: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def view_order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed view of a single order from the pool"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Extract delivery_id from callback data: staff_view_order_{delivery_id}
            delivery_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.get_order_pool(
                    token,
                    filters={
                        'delivery_id': delivery_id,
                        'include_assigned': True,
                        'page': 1,
                        'per_page': 1,
                    }
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            # Find the specific order
            orders = response.data.get('items', [])
            order = orders[0] if orders else None

            if not order:
                await query.edit_message_text(
                    i18n.get('staff.delivery.order_not_found', language),
                    reply_markup=CommonKeyboards.back_button(language, "staff_new_orders")
                )
                return

            # Build detailed view
            order_number = escape_html(order.get('order_number') or i18n.get('staff.common.not_available', language))
            lines = [f"\U0001f4e6 <b>#{order_number}</b>\n"]

            # Items
            items = order.get('items', [])
            if items:
                lines.append(f"<b>{i18n.get('staff.delivery.items', language)}:</b>")
                for item in items:
                    name = escape_html(item.get('product_name', item.get('name', '')))
                    qty = item.get('quantity', 1)
                    price = format_currency(
                        item.get('total_price', item.get('price', 0)),
                        language=language,
                    )
                    lines.append(f"  \u2022 {name} x{qty} \u2014 {price}")
                lines.append("")

            # Customer info
            customer_name = escape_html(order.get('customer_name', ''))
            customer_phone = escape_html(order.get('customer_phone', ''))
            if customer_name or customer_phone:
                contact = f"\U0001f464 {customer_name}"
                if customer_phone:
                    contact += f" | {customer_phone}"
                lines.append(contact)

            # Address
            district = escape_html(order.get('district', ''))
            address = escape_html(order.get('address', ''))
            if district:
                lines.append(f"\U0001f4cd {district}")
            if address:
                lines.append(f"    {address}")
            delivery_instructions = escape_html(order.get('delivery_instructions', ''))
            if delivery_instructions:
                lines.append(f"    \U0001f4dd {delivery_instructions}")

            # Delivery time
            time_slot = escape_html(order.get('time_slot', ''))
            if time_slot:
                lines.append(f"\U0001f550 {time_slot}")

            # Payment
            total = format_currency(order.get('total_amount'), language=language)
            payment = order.get('payment_method', '')
            payment_info = f"\U0001f4b0 {total}"
            if payment:
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language)
                payment_info += f" ({payment_label})"
            lines.append(payment_info)
            if payment == 'cash':
                cod_projection = get_cod_cash_projection(order)
                lines.append(
                    f"\U0001f4b8 {i18n.get('staff.delivery.cash_outstanding_label', language)}: "
                    f"{format_currency(order.get('outstanding_amount'), language=language)}"
                )
                if cod_projection['cod_reserved_prepayment_amount'] > 0:
                    lines.append(
                        f"\U0001f4b3 COD prepaid reserved: "
                        f"{format_currency(cod_projection['cod_reserved_prepayment_amount'], language=language)}"
                    )
                lines.append(
                    f"\U0001f4b5 Cash to collect now: "
                    f"{format_currency(cod_projection['expected_cash_to_collect'], language=language)}"
                )
                payment_status = str(order.get('payment_status') or '').lower()
                if payment_status == 'completed' or cod_projection['expected_cash_to_collect'] <= 0:
                    lines.append(f"\u2705 {i18n.get('staff.delivery.cash_already_collected', language)}")
                elif payment_status == 'partially_paid':
                    lines.append(f"\u2139\ufe0f {i18n.get('staff.delivery.cash_partially_collected', language)}")

            # Delivery notes
            notes = order.get('delivery_notes', '')
            if notes:
                lines.append(f"\U0001f4ac {escape_html(notes)}")

            text = '\n'.join(lines)
            order_id = order.get('order_id')
            status = order.get('status')
            keyboard = DeliveryKeyboards.order_detail_actions(
                language=language,
                delivery_id=delivery_id,
                order_id=order_id,
                can_mark_preparing=(status == 'confirmed'),
                back_callback="staff_new_orders",
            )

            await query.edit_message_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error viewing order details: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def accept_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show acceptance confirmation"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Extract delivery_id: staff_accept_order_{delivery_id}
            delivery_id = int(query.data.split('_')[-1])
            text = i18n.get('staff.delivery.confirm_accept', language)
            keyboard = DeliveryKeyboards.accept_confirm(language, delivery_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing accept confirmation: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_accept(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute order acceptance"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Extract delivery_id: staff_confirm_accept_{delivery_id}
            delivery_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.accept_order(token, delivery_id)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                    return
                if response.status_code == 409:
                    # Already taken by another driver
                    await query.edit_message_text(
                        f"\u274c {i18n.get('staff.delivery.already_taken', language)}",
                        reply_markup=CommonKeyboards.back_button(language, "staff_new_orders")
                    )
                    return
                # For any other failure (e.g. STAFF_DRIVER_COD_BLOCKED,
                # STAFF_MAX_CONCURRENT_REACHED), replace the confirm screen with the
                # resolved error message + a back button so the user can't re-click
                # the stale confirm buttons.
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                await query.edit_message_text(
                    f"\u274c {error_msg}",
                    reply_markup=CommonKeyboards.back_button(language, "staff_new_orders"),
                    parse_mode='HTML',
                )
                return

            # Success
            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.delivery.accepted_success', language)}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error confirming order acceptance: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def pool_pagination(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle pool pagination"""
        query = update.callback_query
        await query.answer()

        try:
            # Extract page: staff_pool_page_{page}
            page = int(query.data.split('_')[-1])
            context.user_data['pool_page'] = page
            await self.show_pool(update, context)

        except Exception as e:
            logger.error(f"Error in pool pagination: {e}", exc_info=True)
            await self._handle_error(update, context)
