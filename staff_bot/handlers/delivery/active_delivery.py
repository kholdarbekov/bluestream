"""
Active Delivery Handler for Staff Bot
Shows and manages deliveries currently assigned to the delivery person.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.delivery import DeliveryKeyboards
from keyboards.common import CommonKeyboards
from utils.formatters import format_delivery_status, format_currency, escape_html
from permissions import require_auth, require_delivery_driver
from i18n import i18n

logger = logging.getLogger(__name__)


class ActiveDeliveryHandler(BaseHandler):
    """Handle active delivery listing and management"""

    @require_auth
    @require_delivery_driver
    async def show_active_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of active deliveries"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_active_deliveries(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            deliveries = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not deliveries:
                text = f"\U0001f69a {i18n.get('staff.delivery.no_active', language)}"
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
            header = (
                f"\U0001f69a <b>{i18n.get('staff.delivery.active_title', language)}</b>\n"
                f"{i18n.get('staff.delivery.active_count', language, count=len(deliveries))}\n"
            )

            if update.callback_query:
                await update.callback_query.edit_message_text(header, parse_mode='HTML')
            else:
                await update.message.reply_text(header, parse_mode='HTML')

            # Send each delivery as a separate message
            for delivery in deliveries:
                status = delivery.get('status', '')
                status_text = format_delivery_status(status, language)
                order_num = escape_html(delivery.get('order_number') or i18n.get('staff.common.not_available', language))

                lines = [
                    f"\U0001f69a <b>#{order_num}</b> \u2014 {status_text}",
                ]

                customer_name = escape_html(delivery.get('customer_name', ''))
                if customer_name:
                    lines.append(f"\U0001f464 {customer_name}")

                address = escape_html(delivery.get('address', ''))
                district = escape_html(delivery.get('district', ''))
                if district:
                    lines.append(f"\U0001f4cd {district}")
                if address:
                    lines.append(f"    {address}")

                total = format_currency(delivery.get('total_amount'), language=language)
                payment = delivery.get('payment_method', '')
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
                if payment_label:
                    lines.append(f"\U0001f4b0 {total} ({payment_label})")
                else:
                    lines.append(f"\U0001f4b0 {total}")

                text = '\n'.join(lines)
                delivery_id = delivery.get('delivery_id') or delivery.get('id')

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"\U0001f4cb {i18n.get('staff.delivery.manage', language)}",
                        callback_data=f"staff_view_active_{delivery_id}"
                    )
                ]])

                target = update.callback_query.message if update.callback_query else update.message
                await target.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing active deliveries: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def view_active_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed management view for a single active delivery"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Extract delivery_id: staff_view_active_{delivery_id}
            delivery_id = int(query.data.split('_')[-1])

            # Fetch active deliveries to find this one
            async with api_client as client:
                response = await client.get_active_deliveries(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            deliveries = response.data if isinstance(response.data, list) else response.data.get('items', [])

            delivery = None
            for d in deliveries:
                if (d.get('delivery_id') or d.get('id')) == delivery_id:
                    delivery = d
                    break

            if not delivery:
                await query.edit_message_text(
                    i18n.get('staff.delivery.not_found', language),
                    reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries")
                )
                return

            # Build detailed view
            status = delivery.get('status', '')
            status_text = format_delivery_status(status, language)
            order_num = escape_html(delivery.get('order_number') or i18n.get('staff.common.not_available', language))

            lines = [
                f"\U0001f69a <b>#{order_num}</b>",
                f"{i18n.get('staff.delivery.current_status', language)}: {status_text}",
                "",
            ]

            # Items
            items = delivery.get('items', [])
            if items:
                lines.append(f"<b>{i18n.get('staff.delivery.items', language)}:</b>")
                for item in items:
                    name = escape_html(item.get('product_name', item.get('name', '')))
                    qty = item.get('quantity', 1)
                    lines.append(f"  \u2022 {name} x{qty}")
                lines.append("")

            # Customer
            customer_name = escape_html(delivery.get('customer_name', ''))
            customer_phone = escape_html(delivery.get('customer_phone', ''))
            if customer_name:
                lines.append(f"\U0001f464 {customer_name}")
            if customer_phone:
                lines.append(f"\U0001f4de {customer_phone}")

            # Address
            address = escape_html(delivery.get('address', ''))
            district = escape_html(delivery.get('district', ''))
            if district:
                lines.append(f"\U0001f4cd {district}")
            if address:
                lines.append(f"    {address}")

            # Payment
            total = format_currency(delivery.get('total_amount'), language=language)
            payment = delivery.get('payment_method', '')
            payment_info = f"\U0001f4b0 {total}"
            if payment:
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language)
                payment_info += f" ({payment_label})"
            lines.append(payment_info)

            # Delivery notes
            notes = delivery.get('delivery_notes', '')
            if notes:
                lines.append(f"\U0001f4ac {escape_html(notes)}")

            # Store delivery info in context for status updates/navigation
            context.user_data['current_delivery'] = {
                'delivery_id': delivery_id,
                'status': status,
                'total_amount': delivery.get('total_amount', 0),
                'payment_method': payment,
                'customer_phone': customer_phone,
                'address': address,
                # Origin: driver's last known point
                'origin_lat': delivery.get('current_location_lat'),
                'origin_lng': delivery.get('current_location_lng'),
                # Destination: order address coordinates
                'destination_lat': delivery.get('destination_latitude'),
                'destination_lng': delivery.get('destination_longitude'),
            }

            text = '\n'.join(lines)
            keyboard = DeliveryKeyboards.active_delivery_actions(language, delivery_id, status)

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error viewing active delivery: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def navigate_to_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open delivery route in Yandex Maps using coordinates."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_info = context.user_data.get('current_delivery', {})
            address = escape_html(delivery_info.get('address', ''))
            destination_lat = delivery_info.get('destination_lat')
            destination_lng = delivery_info.get('destination_lng')
            origin_lat = delivery_info.get('origin_lat')
            origin_lng = delivery_info.get('origin_lng')

            if destination_lat is None or destination_lng is None:
                await query.answer(
                    i18n.get('staff.delivery.no_address', language),
                    show_alert=True
                )
                return

            # Build Yandex route URL: origin~destination (or destination-only)
            if origin_lat is not None and origin_lng is not None:
                maps_url = (
                    f"https://yandex.com/maps/?rtext="
                    f"{origin_lat},{origin_lng}~{destination_lat},{destination_lng}&rtt=auto"
                )
            else:
                maps_url = (
                    f"https://yandex.com/maps/?rtext=~{destination_lat},{destination_lng}&rtt=auto"
                )

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"\U0001f5fa {i18n.get('staff.delivery.open_maps', language)}",
                    url=maps_url
                )
            ], [
                InlineKeyboardButton(
                    f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                    callback_data=f"staff_view_active_{delivery_info.get('delivery_id', 0)}"
                )
            ]])

            await query.edit_message_text(
                f"\U0001f4cd {i18n.get('staff.delivery.navigate_text', language)}\n"
                f"{address}\n"
                f"({destination_lat}, {destination_lng})",
                reply_markup=keyboard,
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error navigating: {e}", exc_info=True)
            await self._handle_error(update, context)
