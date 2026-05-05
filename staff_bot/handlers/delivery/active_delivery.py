"""
Active Delivery Handler for Staff Bot
Shows and manages deliveries currently assigned to the delivery person.
"""
import hashlib
import json
import logging
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import (
    format_delivery_status,
    format_currency,
    escape_html,
    get_cod_cash_projection,
)
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)

# user_data keys for tracking the last render of the "My active deliveries"
# view so we can clean it up before the next render and avoid (a) duplicate
# card stacking on every Optimize/refresh tap, and (b) Telegram "Message is
# not modified" errors from no-op header edits.
_CARDS_KEY = 'active_list_card_ids'           # list[(chat_id, message_id)]
_HEADER_SIG_KEY = 'active_list_header_sig'    # str: sha256(header + buttons)


class ActiveDeliveryHandler(BaseHandler):
    """Handle active delivery listing and management"""

    @staticmethod
    def _compute_render_signature(text: str, keyboard) -> str:
        """Stable hash of message text + button labels/callbacks. We compare
        signatures across renders so we never call edit_message_text with
        identical content (which Telegram rejects as "Message is not
        modified")."""
        kb_repr = []
        if isinstance(keyboard, InlineKeyboardMarkup):
            for row in keyboard.inline_keyboard:
                for btn in row:
                    kb_repr.append(
                        f"{getattr(btn, 'text', '')}|"
                        f"{getattr(btn, 'callback_data', '') or ''}|"
                        f"{getattr(btn, 'url', '') or ''}"
                    )
        payload = text + '||' + json.dumps(kb_repr)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    async def _delete_previous_card_messages(context: ContextTypes.DEFAULT_TYPE):
        """Best-effort cleanup of card messages tracked from the prior render.

        Failure modes (already deleted, too old, chat changed, bot lost
        permission) are expected and only surface as DEBUG logs — there's no
        useful recovery and they don't affect correctness of the new render.
        """
        bot = context.bot
        for chat_id, msg_id in context.user_data.get(_CARDS_KEY, []) or []:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Cleanup delete_message {chat_id}/{msg_id} skipped: {exc}")
        context.user_data[_CARDS_KEY] = []

    async def _render_header(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        keyboard,
    ):
        """Render or update the header message.

        - For inline-button callbacks: edit the existing message *only* when
          the new content actually differs (signature compare). Identical
          content → skip the API call entirely.
        - For fresh entries (typed command, menu tap): send a new message.

        Stores the rendered signature in `user_data` for the next render.
        """
        new_sig = self._compute_render_signature(text, keyboard)
        old_sig = context.user_data.get(_HEADER_SIG_KEY)

        if update.callback_query:
            if old_sig != new_sig:
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            # else: identical to current, no API call needed.
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

        context.user_data[_HEADER_SIG_KEY] = new_sig

    @require_auth
    @require_delivery_driver
    async def show_active_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of active deliveries.

        Renders as a header message + N per-delivery card messages. To keep
        the chat tidy and avoid duplicate card stacking on re-renders (e.g.
        when the user taps "Optimize routes"), we track the message IDs
        from the previous render in `context.user_data` and delete them
        before sending fresh cards.
        """
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

            payload = response.data if isinstance(response.data, dict) else {}
            deliveries = (
                payload.get('items')
                if isinstance(payload, dict) and 'items' in payload
                else (response.data if isinstance(response.data, list) else [])
            )
            location_status = payload.get('location_status', 'fresh') if isinstance(payload, dict) else 'fresh'

            # Reclaim previous-render card messages BEFORE sending the new
            # ones so the chat doesn't accumulate duplicates with each refresh.
            await self._delete_previous_card_messages(context)

            if not deliveries:
                text = f"\U0001f69a {i18n.get('staff.delivery.no_active', language)}"
                # Show share-location prompt even when empty if location is missing.
                keyboard = DeliveryKeyboards.active_list_top_actions(
                    language, show_share_location=(location_status != 'fresh')
                ) if location_status != 'fresh' else CommonKeyboards.back_button(language)

                await self._render_header(update, context, text, keyboard)
                return

            # Header
            header_lines = [
                f"\U0001f69a <b>{i18n.get('staff.delivery.active_title', language)}</b>",
                i18n.get('staff.delivery.active_count', language, count=len(deliveries)),
            ]
            if location_status == 'missing':
                # Hard precondition not met \u2014 optimization is OFF until the
                # driver shares location. The list below is unsorted (just
                # in claim order) and there's no ETA. Be explicit so the
                # driver knows what to do.
                header_lines.append("")
                header_lines.append(
                    f"\u26a0\ufe0f <b>{i18n.get('staff.delivery.location_required_notice', language)}</b>"
                )
            elif location_status == 'stale':
                # We have a location but it's older than the freshness
                # threshold \u2014 the sequence is computed from the last
                # known position. Suggest a re-share for accuracy.
                header_lines.append("")
                header_lines.append(
                    f"\u2139\ufe0f {i18n.get('staff.delivery.location_stale_notice', language)}"
                )
            header = '\n'.join(header_lines) + '\n'

            top_actions = DeliveryKeyboards.active_list_top_actions(
                language, show_share_location=(location_status != 'fresh')
            )

            await self._render_header(update, context, header, top_actions)

            # Send each delivery as a separate message and track the IDs so
            # we can reclaim them on the next render.
            new_card_ids = []
            for delivery in deliveries:
                status = delivery.get('status', '')
                status_text = format_delivery_status(status, language)
                order_num = escape_html(delivery.get('order_number') or i18n.get('staff.common.not_available', language))

                lines = []
                # Only show the "Next stop \u00b7 ETA \u00b7 km" badge when we have a
                # real driver location to compute it from. When location is
                # missing/stale, the ETA would be measured from the depot /
                # city centre \u2014 confusing rather than useful, so we suppress it.
                if delivery.get('is_next') and location_status == 'fresh':
                    eta = delivery.get('eta_minutes_from_current_location')
                    km = delivery.get('distance_km_to_next')
                    next_parts = [
                        f"\U0001f4cd <b>{i18n.get('staff.delivery.next_stop', language)}</b>"
                    ]
                    if eta is not None:
                        next_parts.append(
                            f"\u23f1 {i18n.get('staff.delivery.eta_minutes', language, minutes=int(eta))}"
                        )
                    if km is not None:
                        next_parts.append(
                            f"\U0001f4cf {i18n.get('staff.delivery.distance_km', language, km=km)}"
                        )
                    lines.append(' \u00b7 '.join(next_parts))

                position = delivery.get('route_position')
                position_prefix = f"{position + 1}. " if isinstance(position, int) else ""
                lines.append(
                    f"\U0001f69a <b>{position_prefix}#{order_num}</b> \u2014 {status_text}"
                )

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
                if payment == 'cash':
                    cod_projection = get_cod_cash_projection(delivery)
                    lines.append(
                        f"\U0001f9fe {i18n.get('staff.delivery.cash_collected_label', language)}: "
                        f"{format_currency(delivery.get('amount_collected'), language=language)}"
                    )
                    lines.append(
                        f"\U0001f4b8 {i18n.get('staff.delivery.cash_outstanding_label', language)}: "
                        f"{format_currency(delivery.get('outstanding_amount'), language=language)}"
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
                    payment_status = str(delivery.get('payment_status') or '').lower()
                    if payment_status == 'completed' or cod_projection['expected_cash_to_collect'] <= 0:
                        lines.append(f"\u2705 {i18n.get('staff.delivery.cash_already_collected', language)}")
                    elif payment_status == 'partially_paid':
                        lines.append(f"\u2139\ufe0f {i18n.get('staff.delivery.cash_partially_collected', language)}")

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
                sent = await target.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                new_card_ids.append((sent.chat_id, sent.message_id))

            # Persist the freshly-sent card IDs so the next render can clean
            # them up (and avoid stacking duplicates across Optimize taps).
            context.user_data[_CARDS_KEY] = new_card_ids

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
            delivery_instructions = escape_html(delivery.get('delivery_instructions', ''))
            if delivery_instructions:
                lines.append(f"    \U0001f4dd {delivery_instructions}")

            # Payment
            total = format_currency(delivery.get('total_amount'), language=language)
            payment = delivery.get('payment_method', '')
            payment_info = f"\U0001f4b0 {total}"
            if payment:
                payment_label = i18n.get(f'staff.delivery.payment.{payment}', language)
                payment_info += f" ({payment_label})"
            lines.append(payment_info)
            if payment == 'cash':
                cod_projection = get_cod_cash_projection(delivery)
                lines.append(
                    f"\U0001f9fe {i18n.get('staff.delivery.cash_collected_label', language)}: "
                    f"{format_currency(delivery.get('amount_collected'), language=language)}"
                )
                lines.append(
                    f"\U0001f4b8 {i18n.get('staff.delivery.cash_outstanding_label', language)}: "
                    f"{format_currency(delivery.get('outstanding_amount'), language=language)}"
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
                payment_status = str(delivery.get('payment_status') or '').lower()
                if payment_status == 'completed' or cod_projection['expected_cash_to_collect'] <= 0:
                    lines.append(f"\u2705 {i18n.get('staff.delivery.cash_already_collected', language)}")
                elif payment_status == 'partially_paid':
                    lines.append(f"\u2139\ufe0f {i18n.get('staff.delivery.cash_partially_collected', language)}")

            # Delivery notes
            notes = delivery.get('delivery_notes', '')
            if notes:
                lines.append(f"\U0001f4ac {escape_html(notes)}")

            # Store delivery info in context for status updates/navigation
            context.user_data['current_delivery'] = {
                'delivery_id': delivery_id,
                'order_id': delivery.get('order_id'),
                'customer_id': delivery.get('customer_id'),
                'status': status,
                'total_amount': delivery.get('total_amount', 0),
                'payment_method': payment,
                'payment_status': delivery.get('payment_status'),
                'amount_collected': delivery.get('amount_collected', 0),
                'outstanding_amount': delivery.get('outstanding_amount', 0),
                'cod_reserved_prepayment_amount': delivery.get('cod_reserved_prepayment_amount', 0),
                'expected_cash_to_collect': delivery.get('expected_cash_to_collect', 0),
                'customer_phone': customer_phone,
                'address': address,
                # Origin: driver's last known point
                'origin_lat': delivery.get('current_location_lat'),
                'origin_lng': delivery.get('current_location_lng'),
                # Destination: order address coordinates
                'destination_lat': delivery.get('destination_latitude'),
                'destination_lng': delivery.get('destination_longitude'),
                # Returnable bottles
                'expected_returnable_bottles': delivery.get('expected_returnable_bottles', 0),
            }

            text = '\n'.join(lines)
            keyboard = DeliveryKeyboards.active_delivery_actions(language, delivery_id, status)

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error viewing active delivery: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def optimize_routes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually re-run route optimization and re-render the active list.

        If the driver has never shared their location, the backend refuses
        with 412 LOCATION_REQUIRED — we surface a clear "share your location
        first" message + the location-request keyboard rather than silently
        rendering a city-centre fallback.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await query.answer()
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.optimize_route(token)

            # Driver location precondition not met — prompt to share rather
            # than silently optimizing from a fallback origin.
            if (
                not response.success
                and response.status_code == 412
                and (response.error_code == "LOCATION_REQUIRED" or "LOCATION" in (response.error or "").upper())
            ):
                await query.answer(
                    i18n.get('staff.delivery.share_location_first_toast', language),
                    show_alert=True,
                )
                prompt = i18n.get('staff.delivery.share_location_prompt', language)
                button_text = i18n.get('staff.delivery.share_location_button', language)
                await query.message.reply_text(
                    prompt,
                    reply_markup=CommonKeyboards.location_request(language, button_text),
                )
                return

            if not response.success:
                await query.answer()
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            # Optimization ran successfully — confirm and re-render.
            await query.answer(
                i18n.get('staff.delivery.route_updated_toast', language)
            )
            await self.show_active_deliveries(update, context)
        except Exception as e:
            logger.error(f"Error in optimize_routes: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def share_location_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply with a Telegram location-request keyboard so the driver can
        share their current location in one tap. Also explains how to enable
        live-location sharing for the rest of the route.
        """
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        prompt = i18n.get('staff.delivery.share_location_prompt', language)
        button_text = i18n.get('staff.delivery.share_location_button', language)
        keyboard = CommonKeyboards.location_request(language, button_text)
        await query.message.reply_text(prompt, reply_markup=keyboard)

    @require_auth
    @require_delivery_driver
    async def decline_suggestion(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Driver declined a pool-insertion suggestion — just dismiss the message."""
        query = update.callback_query
        language = await self._get_language(update, context)
        await query.answer()
        try:
            await query.edit_message_text(
                f"➖ {i18n.get('staff.delivery.suggestion_declined', language)}"
            )
        except Exception as e:
            logger.warning(f"Failed to edit declined-suggestion message: {e}")

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
