"""
Delivery History & Stats Handler for Staff Bot
Shows delivery history and performance statistics for delivery persons.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.common import CommonKeyboards
from utils.formatters import format_delivery_status, format_delivery_stats, format_currency
from permissions import require_auth, require_delivery_driver
from i18n import i18n

logger = logging.getLogger(__name__)


class HistoryHandler(BaseHandler):
    """Handle delivery history and stats display"""

    @require_auth
    @require_delivery_driver
    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show paginated delivery history"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            page = context.user_data.get('history_page', 1)
            async with api_client as client:
                response = await client.get_delivery_history(
                    token, params={'page': page, 'per_page': 10}
                )

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_error(update, response.error, language)
                return

            data = response.data or {}
            deliveries = data.get('items', [])
            pagination = data.get('pagination', {})

            if not deliveries:
                text = f"\U0001f4cb {i18n.get('staff.delivery.no_history', language)}"
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

            # Build history list
            lines = [
                f"\U0001f4cb <b>{i18n.get('staff.delivery.history_title', language)}</b>\n"
            ]

            for delivery in deliveries:
                status = delivery.get('status', '')
                status_text = format_delivery_status(status, language)
                order_num = delivery.get('order_number', 'N/A')
                district = delivery.get('district', '')
                total = format_currency(delivery.get('total_amount'))
                date = delivery.get('delivered_at') or delivery.get('updated_at', '')
                if date and isinstance(date, str) and len(date) > 10:
                    date = date[:10]

                lines.append(
                    f"{status_text} <b>#{order_num}</b>"
                )
                if district:
                    lines.append(f"  \U0001f4cd {district} | {total}")
                else:
                    lines.append(f"  \U0001f4b0 {total}")
                if date:
                    lines.append(f"  \U0001f4c5 {date}")
                lines.append("")

            text = '\n'.join(lines)

            # Pagination
            keyboard_rows = []
            total_pages = pagination.get('pages', 1)
            if total_pages > 1:
                page_buttons = CommonKeyboards.pagination(
                    language, page, total_pages, 'staff_history'
                )
                keyboard_rows.append(page_buttons)

            keyboard_rows.append([InlineKeyboardButton(
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main"
            )])

            keyboard = InlineKeyboardMarkup(keyboard_rows)

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error showing history: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def history_pagination(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle history pagination"""
        query = update.callback_query
        await query.answer()

        try:
            page = int(query.data.split('_')[-1])
            context.user_data['history_page'] = page
            await self.show_history(update, context)
        except Exception as e:
            logger.error(f"Error in history pagination: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show delivery performance statistics"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_delivery_stats(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_error(update, response.error, language)
                return

            stats = response.data or {}
            text = format_delivery_stats(stats, language)

            # Period selection buttons
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.day', language),
                        callback_data="staff_stats_period_day"
                    ),
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.week', language),
                        callback_data="staff_stats_period_week"
                    ),
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.month', language),
                        callback_data="staff_stats_period_month"
                    ),
                ],
                [InlineKeyboardButton(
                    f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                    callback_data="staff_back_to_main"
                )]
            ])

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error showing stats: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def change_stats_period(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change stats period and refresh"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_stats_period_{period}
            period = query.data.split('_')[-1]

            async with api_client as client:
                response = await client.get_delivery_stats(
                    token, params={'period': period}
                )

            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            stats = response.data or {}
            text = format_delivery_stats(stats, language)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.day', language),
                        callback_data="staff_stats_period_day"
                    ),
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.week', language),
                        callback_data="staff_stats_period_week"
                    ),
                    InlineKeyboardButton(
                        i18n.get('staff.stats.period.month', language),
                        callback_data="staff_stats_period_month"
                    ),
                ],
                [InlineKeyboardButton(
                    f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                    callback_data="staff_back_to_main"
                )]
            ])

            await query.edit_message_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error changing stats period: {e}", exc_info=True)
            await self._handle_error(update, context)
