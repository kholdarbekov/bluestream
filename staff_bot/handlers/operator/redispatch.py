"""
Re-dispatch Failed Delivery Handler for Staff Bot (operators / dispatchers).

Lists deliveries currently in FAILED status and lets an operator return a chosen
one to the unassigned pool (clearing the driver and restoring the order) so a
driver can re-claim it. Mirrors the structure of RecentOrdersHandler.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import format_currency, escape_html
from staff_bot.permissions import require_auth, require_operator
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)


class RedispatchHandler(BaseHandler):
    """Operator flow to re-dispatch failed deliveries back to the pool."""

    @require_auth
    @require_operator
    async def show_failed_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show failed deliveries, each with a re-dispatch button."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_failed_deliveries(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            deliveries = (
                response.data if isinstance(response.data, list) else (response.data or {}).get('items', [])
            )

            if not deliveries:
                text = f"✅ {i18n.get('staff.redispatch.none', language)}"
                keyboard = CommonKeyboards.back_button(language)
                if update.callback_query:
                    await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            header = (
                f"🔄 <b>{i18n.get('staff.redispatch.title', language)}</b>\n"
                f"{i18n.get('staff.redispatch.pick', language)}"
            )
            if update.callback_query:
                await update.callback_query.edit_message_text(header, parse_mode='HTML')
            else:
                await update.message.reply_text(header, parse_mode='HTML')

            target = update.callback_query.message if update.callback_query else update.message
            for d in deliveries[:15]:
                order_num = escape_html(d.get('order_number') or i18n.get('staff.common.not_available', language))
                customer = escape_html(d.get('customer_name', ''))
                address = escape_html(d.get('address', ''))
                total = format_currency(d.get('total_amount'), language=language)
                reason = escape_html(d.get('failed_delivery_reason') or '')
                attempts = d.get('delivery_attempts') or 0

                lines = [f"📦 <b>#{order_num}</b>"]
                if customer:
                    lines.append(f"👤 {customer}")
                if address:
                    lines.append(f"📍 {address}")
                lines.append(f"💰 {total}")
                if reason:
                    lines.append(f"❗ {reason}")
                lines.append(f"🔁 {i18n.get('staff.redispatch.attempts', language)}: {attempts}")

                button = InlineKeyboardButton(
                    f"🔄 {i18n.get('staff.redispatch.button', language)}",
                    callback_data=f"staff_redispatch_do_{d.get('delivery_id')}",
                )
                await target.reply_text(
                    '\n'.join(lines),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([[button]]),
                )

        except Exception as e:
            logger.error(f"Error showing failed deliveries: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def redispatch_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Re-dispatch the chosen failed delivery back to the pool."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        query = update.callback_query
        try:
            delivery_id = int(query.data.rsplit('_', 1)[-1])
        except (ValueError, AttributeError):
            await self._handle_error(update, context)
            return

        try:
            async with api_client as client:
                response = await client.redispatch_delivery(token, delivery_id)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            await query.edit_message_text(
                f"✅ {i18n.get('staff.redispatch.success', language)}",
                parse_mode='HTML',
                reply_markup=CommonKeyboards.back_button(language),
            )

        except Exception as e:
            logger.error(f"Error re-dispatching delivery: {e}", exc_info=True)
            await self._handle_error(update, context)
