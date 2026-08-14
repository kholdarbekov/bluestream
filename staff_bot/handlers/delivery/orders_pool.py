"""
Orders Pool Handler for Staff Bot
Shows unassigned orders available for pickup by delivery persons.
"""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import (
    escape_html,
    format_currency,
    format_money_block,
    format_order_card,
)
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)


class OrdersPoolHandler(BaseHandler):
    """Handle order pool browsing and acceptance"""

    @staticmethod
    def _effective_page(user_data: dict, *, reset: bool) -> int:
        """Resolve which pool page to fetch.

        Fresh entry into the "New Orders" menu (reset=True) always starts at
        page 1 — otherwise a ``pool_page`` left over from an earlier pagination
        tap pins the driver to a stale page and they never see page 1 again.
        Pagination taps pass reset=False to honour the page the driver chose.
        """
        if reset:
            user_data['pool_page'] = 1
        page = user_data.get('pool_page', 1)
        if not isinstance(page, int) or page < 1:
            page = 1
            user_data['pool_page'] = 1
        return page

    @staticmethod
    def _clamp_page(user_data: dict, total_pages: int) -> int:
        """Clamp the stored page into ``[1, total_pages]`` (min 1) and persist it.

        A pagination button rendered while the pool was larger can request a
        page that no longer exists after other drivers claimed orders; without
        this the driver sees an empty list with no way back to page 1.
        """
        pages = total_pages if isinstance(total_pages, int) and total_pages >= 1 else 1
        page = user_data.get('pool_page', 1)
        if not isinstance(page, int) or page < 1:
            page = 1
        clamped = min(page, pages)
        user_data['pool_page'] = clamped
        return clamped

    async def _fetch_pool_page(self, token, page):
        """Fetch a single page of the unassigned-order pool."""
        async with api_client as client:
            return await client.get_order_pool(token, filters={'page': page, 'per_page': 10})

    @require_auth
    @require_delivery_driver
    async def show_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE, reset_page: bool = True):
        """Show available orders pool.

        ``reset_page`` defaults to True so every fresh open of the menu starts
        at page 1; the pagination handler passes reset_page=False to keep the
        page the driver navigated to.
        """
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            page = self._effective_page(context.user_data, reset=reset_page)
            response = await self._fetch_pool_page(token, page)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            pagination = response.data.get('pagination', {})
            # A stale page beyond the current range (pool shrank since the
            # button was rendered) is clamped back into range and re-fetched,
            # so the driver never lands on a dead-end empty page.
            clamped = self._clamp_page(context.user_data, pagination.get('pages', 1) or 1)
            if clamped != page:
                page = clamped
                response = await self._fetch_pool_page(token, page)
                if not response.success:
                    if response.status_code == 401:
                        await self._handle_auth_error(update, language)
                    else:
                        await self._handle_api_response_error(update, response, language)
                    return
                pagination = response.data.get('pagination', {})

            orders = response.data.get('items', [])

            if not orders:
                text = f"📦 {i18n.get('staff.delivery.pool_empty', language)}"
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
            header = f"📦 <b>{i18n.get('staff.delivery.pool_title', language)}</b>"
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
            lines = [f"📦 <b>#{order_number}</b>\n"]

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
                    lines.append(f"  • {name} x{qty} — {price}")
                lines.append("")

            # Customer info
            customer_name = escape_html(order.get('customer_name', ''))
            customer_phone = escape_html(order.get('customer_phone', ''))
            if customer_name or customer_phone:
                contact = f"👤 {customer_name}"
                if customer_phone:
                    contact += f" | {customer_phone}"
                lines.append(contact)

            # Address
            district = escape_html(order.get('district', ''))
            address = escape_html(order.get('address', ''))
            if district:
                lines.append(f"📍 {district}")
            if address:
                lines.append(f"    {address}")
            delivery_instructions = escape_html(order.get('delivery_instructions', ''))
            if delivery_instructions:
                lines.append(f"    📝 {delivery_instructions}")

            # Delivery time
            time_slot = escape_html(order.get('time_slot', ''))
            if time_slot:
                lines.append(f"🕐 {time_slot}")

            # Payment
            total = format_currency(order.get('total_amount'), language=language)
            payment = order.get('payment_method', '')
            payment_info = f"💰 {total}"
            if payment:
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language)
                payment_info += f" ({payment_label})"
            lines.append(payment_info)
            # SSOT: the same money block format_order_card renders. This used to
            # be a third hand-rolled copy gated on `payment == 'cash'`, so
            # widening the formatter left the pool showing nothing owed on an
            # order that owed money (plan 2026-08-08-open-receivable-ssot).
            lines.extend(format_money_block(order, language))

            # Delivery notes
            notes = order.get('delivery_notes', '')
            if notes:
                lines.append(f"💬 {escape_html(notes)}")

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
                        f"❌ {i18n.get('staff.delivery.already_taken', language)}",
                        reply_markup=CommonKeyboards.back_button(language, "staff_new_orders")
                    )
                    return
                error_code = getattr(response, 'error_code', None)
                if error_code == 'BOTTLE_SESSION_REQUIRED':
                    # Driver has no bottle session — offer to start one or join a colleague's
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            i18n.get('staff.bottles.start_session', language),
                            callback_data='bottles_start_session',
                        )],
                        [InlineKeyboardButton(
                            i18n.get('staff.bottles.join_session', language),
                            callback_data='bottles_join_session',
                        )],
                        [InlineKeyboardButton(
                            i18n.get('staff.back', language),
                            callback_data='staff_new_orders',
                        )],
                    ])
                    await query.edit_message_text(
                        f"⚠️ {i18n.get('staff.bottles.session_required_to_accept', language)}",
                        reply_markup=keyboard,
                        parse_mode='HTML',
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
                    error_code=error_code,
                )
                await query.edit_message_text(
                    f"❌ {error_msg}",
                    reply_markup=CommonKeyboards.back_button(language, "staff_new_orders"),
                    parse_mode='HTML',
                )
                return

            # Success
            await query.edit_message_text(
                f"✅ {i18n.get('staff.delivery.accepted_success', language)}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

            # No location prompt here. Accepting an order used to send a second
            # message whose reply keyboard replaced the driver's entire main
            # menu, on every single accept. The route card refreshes itself via
            # the silent route-updated webhook, and a driver whose fix has aged
            # taps "Optimize route" on the card — one tap, on their terms.

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
            await self.show_pool(update, context, reset_page=False)

        except Exception as e:
            logger.error(f"Error in pool pagination: {e}", exc_info=True)
            await self._handle_error(update, context)
