"""
Recent Orders Handler for Staff Bot
Shows orders recently created by the operator.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.common import CommonKeyboards
from utils.formatters import format_currency, escape_html
from permissions import require_auth, require_operator
from i18n import i18n

logger = logging.getLogger(__name__)


class RecentOrdersHandler(BaseHandler):
    """Handle recent orders listing for operators"""

    @require_auth
    @require_operator
    async def show_recent_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show orders recently created by this operator"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_recent_operator_orders(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            orders = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not orders:
                text = f"\U0001f4cb {i18n.get('staff.operator.no_recent_orders', language)}"
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

            # Header
            header = f"\U0001f4cb <b>{i18n.get('staff.operator.recent_orders_title', language)}</b>\n"

            if update.callback_query:
                await update.callback_query.edit_message_text(header, parse_mode='HTML')
            else:
                await update.message.reply_text(header, parse_mode='HTML')

            # Show each order
            for order in orders[:15]:
                order_num = escape_html(order.get('order_number') or i18n.get('staff.common.not_available', language))
                status = order.get('status', '')
                status_label = i18n.get(f'staff.order.status.{status}', language) if status else ''
                customer_name = escape_html(order.get('customer_name', ''))
                total = format_currency(order.get('total_amount'), language=language)
                created = escape_html(order.get('created_at', ''))
                if created and isinstance(created, str) and len(created) > 16:
                    created = created[:16].replace('T', ' ')

                lines = [
                    f"\U0001f4e6 <b>#{order_num}</b> \u2014 {escape_html(status_label or status)}",
                ]
                if customer_name:
                    lines.append(f"\U0001f464 {customer_name}")
                lines.append(f"\U0001f4b0 {total}")
                if created:
                    lines.append(f"\U0001f4c5 {created}")

                text = '\n'.join(lines)
                target = update.callback_query.message if update.callback_query else update.message
                await target.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing recent orders: {e}", exc_info=True)
            await self._handle_error(update, context)
