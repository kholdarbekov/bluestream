"""
Operator view-only order pool handler.
Operators can browse pool items and mark confirmed orders as preparing.
"""
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from keyboards.common import CommonKeyboards
from permissions import require_auth, require_operator
from utils.formatters import format_currency, format_order_card, escape_html

logger = logging.getLogger(__name__)


class OperatorOrdersPoolViewHandler(BaseHandler):
    """Read-only pool view for operators."""

    @require_auth
    @require_operator
    async def show_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show unassigned order pool for operators (read-only)."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            page = context.user_data.get('op_pool_page', 1)
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
            total = pagination.get('total', len(orders))

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

            header = f"\U0001f4e6 <b>{i18n.get('staff.operator.pool_title', language)}</b>"
            header += f"\n{i18n.get('staff.delivery.pool_count', language, count=total)}\n"

            if update.callback_query:
                await update.callback_query.edit_message_text(header, parse_mode='HTML')
            else:
                await update.message.reply_text(header, parse_mode='HTML')

            for order in orders:
                order_id = order.get('order_id')
                if not order_id:
                    continue

                card = format_order_card(order, language)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"\U0001f440 {i18n.get('staff.delivery.view_details', language)}",
                        callback_data=f"staff_op_view_order_{order_id}"
                    )
                ]])
                target = update.callback_query.message if update.callback_query else update.message
                await target.reply_text(card, reply_markup=keyboard, parse_mode='HTML')

            total_pages = pagination.get('pages', 1)
            if total_pages > 1:
                page_buttons = CommonKeyboards.pagination(language, page, total_pages, 'staff_op_pool')
                page_keyboard = InlineKeyboardMarkup([page_buttons])
                target = update.callback_query.message if update.callback_query else update.message
                await target.reply_text(
                    f"{i18n.get('staff.page', language)} {page}/{total_pages}",
                    reply_markup=page_keyboard
                )
        except Exception as e:
            logger.error(f"Error showing operator pool: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def view_order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed view for an order from the pool."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            order_id = int(query.data.split('_')[-1])
            async with api_client as client:
                response = await client.get_order_pool(
                    token,
                    filters={
                        'order_id': order_id,
                        'include_assigned': True,
                        'page': 1,
                        'per_page': 1,
                    }
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            items = response.data.get('items', [])
            if not items:
                await query.edit_message_text(
                    i18n.get('staff.delivery.order_not_found', language),
                    reply_markup=CommonKeyboards.back_button(language, "staff_op_new_orders")
                )
                return

            order = items[0]
            status = order.get('status', '')
            status_label = i18n.get(f'staff.order.status.{status}', language) if status else ''
            delivery_person_name = order.get('delivery_person_name', '')

            order_number = escape_html(order.get('order_number') or i18n.get('staff.common.not_available', language))
            lines = [f"\U0001f4e6 <b>#{order_number}</b>"]
            if status_label:
                lines.append(
                    f"{i18n.get('staff.delivery.current_status', language)}: {escape_html(status_label)}"
                )
            if delivery_person_name:
                lines.append(
                    f"\U0001f464 {i18n.get('staff.operator.assigned_to', language)}: {escape_html(delivery_person_name)}"
                )
            lines.append("")

            pool_items = order.get('items', [])
            if pool_items:
                lines.append(f"<b>{i18n.get('staff.delivery.items', language)}:</b>")
                for item in pool_items:
                    name = item.get('product_name', '')
                    qty = item.get('quantity', 1)
                    price = format_currency(item.get('total_price', 0), language=language)
                    lines.append(f"  \u2022 {escape_html(name)} x{qty} - {price}")
                lines.append("")

            customer_name = escape_html(order.get('customer_name', ''))
            customer_phone = escape_html(order.get('customer_phone', ''))
            if customer_name or customer_phone:
                contact = f"\U0001f464 {customer_name}"
                if customer_phone:
                    contact += f" | {customer_phone}"
                lines.append(contact)

            district = escape_html(order.get('district', ''))
            address = escape_html(order.get('address', ''))
            if district:
                lines.append(f"\U0001f4cd {district}")
            if address:
                lines.append(f"    {address}")

            total = format_currency(order.get('total_amount'), language=language)
            payment = order.get('payment_method', '')
            payment_text = f"\U0001f4b0 {total}"
            if payment:
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language)
                payment_text += f" ({payment_label})"
            lines.append(payment_text)

            notes = order.get('delivery_notes', '')
            if notes:
                lines.append(f"\U0001f4ac {escape_html(notes)}")

            keyboard_rows = []
            if status == 'confirmed':
                keyboard_rows.append([InlineKeyboardButton(
                    f"\U0001f6e0\ufe0f {i18n.get('staff.delivery.mark_preparing', language)}",
                    callback_data=f"staff_op_mark_preparing_{order_id}"
                )])
            keyboard_rows.append([InlineKeyboardButton(
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data="staff_op_new_orders"
            )])

            await query.edit_message_text(
                '\n'.join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard_rows),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error showing operator pool details: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def mark_preparing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mark order as preparing from operator read-only pool."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            order_id = int(query.data.split('_')[-1])
            async with api_client as client:
                response = await client.mark_order_preparing(token, order_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.delivery.marked_preparing', language)}",
                reply_markup=CommonKeyboards.back_button(language, "staff_op_new_orders"),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error marking order preparing from operator pool: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def pool_pagination(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle operator pool pagination."""
        query = update.callback_query
        await query.answer()
        try:
            page = int(query.data.split('_')[-1])
            context.user_data['op_pool_page'] = page
            await self.show_pool(update, context)
        except Exception as e:
            logger.error(f"Error paginating operator pool: {e}", exc_info=True)
            await self._handle_error(update, context)
