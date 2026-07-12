"""
Active Delivery Handler for Staff Bot
Shows and manages deliveries currently assigned to the delivery person.
"""
import asyncio
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
    escape_html,
    format_active_delivery_summary,
)
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)

# user_data keys for tracking the last render of the "My active deliveries"
# view so we can clean it up before the next render and avoid (a) duplicate
# card stacking on every Optimize/refresh tap, and (b) Telegram "Message is
# not modified" errors from no-op header edits.
_CARDS_KEY = 'active_list_card_ids'           # list[(chat_id, message_id)]
_HEADER_KEY = 'active_list_header_id'         # tuple[int, int]: (chat_id, message_id)
_HEADER_SIG_KEY = 'active_list_header_sig'    # str: sha256(header + buttons)
# Per-user asyncio.Lock that serializes the read-delete-render-store cycle
# below. PTB normally serializes updates per-user through its update queue,
# but webhook-pushed messages and any future `concurrent_updates=True` config
# would bypass that — without this lock, two concurrent `show_active_deliveries`
# calls would each read the same `_CARDS_KEY` snapshot, both delete the same
# message IDs (the second deletion races into "message not found"), and both
# write their own card list back, leaking the loser's cards as duplicates.
_RENDER_LOCK_KEY = 'active_list_render_lock'  # asyncio.Lock


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

        The header's (chat_id, message_id) is tracked in
        ``user_data[_HEADER_KEY]`` independently from the per-delivery
        cards in ``_CARDS_KEY``. That separation matters because
        `view_active_delivery` edits a card *in place* into the
        delivery-detail view without retracking — so when the driver
        taps the detail view's ⬅️ Back button (callback
        `staff_active_deliveries`), `update.callback_query.message` is a
        soon-to-be-deleted card, NOT the header. The previous design
        treated the callback's source message AS the header and broke
        with `BadRequest: Message to edit not found` because
        `_delete_previous_card_messages` had already removed it by the
        time we tried to edit.

        Behaviour:

        - Callback updates with a tracked header in this chat: edit the
          tracked header in place. Skip the API call entirely when the
          new signature matches the last rendered one (Telegram rejects
          true no-op edits with `Message is not modified`). If the edit
          fails — header was deleted, too old, chat changed, bot lost
          permission — fall back to sending a fresh header and re-track
          its id; the failure is an expected recovery path, logged at
          debug only.

        - Callback updates with no tracked header in this chat (first
          callback after a context reset, or a cross-chat dispatcher
          mistake): send a fresh header and track its id.

        - Fresh entries (typed command, reply-keyboard menu tap): always
          send a new message and overwrite the tracked header id.
        """
        new_sig = self._compute_render_signature(text, keyboard)
        old_sig = context.user_data.get(_HEADER_SIG_KEY)
        header_loc = context.user_data.get(_HEADER_KEY)

        if update.callback_query:
            src_msg = update.callback_query.message
            chat_id = src_msg.chat.id if src_msg and src_msg.chat else None

            same_chat_header = (
                header_loc is not None
                and chat_id is not None
                and header_loc[0] == chat_id
            )

            if same_chat_header:
                if old_sig == new_sig:
                    # Tracked header already shows this exact content.
                    return
                try:
                    await context.bot.edit_message_text(
                        chat_id=header_loc[0],
                        message_id=header_loc[1],
                        text=text,
                        reply_markup=keyboard,
                        parse_mode='HTML',
                    )
                    context.user_data[_HEADER_SIG_KEY] = new_sig
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        f"Header edit failed ({exc}); sending fresh header"
                    )

            sent = await update.callback_query.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )
        else:
            sent = await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

        context.user_data[_HEADER_KEY] = (sent.chat_id, sent.message_id)
        context.user_data[_HEADER_SIG_KEY] = new_sig

    @staticmethod
    def _get_render_lock(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
        """Return the per-user lock that serializes card list renders.

        `setdefault` is the right primitive here: under CPython the GIL makes
        the dict slot lookup-or-set atomic enough that we won't end up with
        two distinct Lock instances for the same user, and even if we did
        the worst case is one extra render which the signature compare in
        `_render_header` would still no-op.
        """
        lock = context.user_data.get(_RENDER_LOCK_KEY)
        if lock is None:
            lock = asyncio.Lock()
            context.user_data[_RENDER_LOCK_KEY] = lock
        return lock

    @require_auth
    @require_delivery_driver
    async def show_active_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of active deliveries.

        Renders as a header message + N per-delivery card messages. To keep
        the chat tidy and avoid duplicate card stacking on re-renders (e.g.
        when the user taps "Optimize routes"), we track the message IDs
        from the previous render in `context.user_data` and delete them
        before sending fresh cards.

        The render is wrapped in a per-user `asyncio.Lock` so concurrent
        invocations (e.g. user tap + webhook-driven re-render) cannot both
        read the same `_CARDS_KEY` snapshot and stack duplicate cards.
        """
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        async with self._get_render_lock(context):
            await self._render_active_deliveries(update, context, language, token)

    async def _render_active_deliveries(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
        token: str,
    ):
        """Inner render body — must run under the per-user render lock."""
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
                text = f"🚚 {i18n.get('staff.delivery.no_active', language)}"
                # Show share-location prompt even when empty if location is missing.
                keyboard = DeliveryKeyboards.active_list_top_actions(
                    language, show_share_location=(location_status != 'fresh')
                ) if location_status != 'fresh' else CommonKeyboards.back_button(language)

                await self._render_header(update, context, text, keyboard)
                return

            # Header
            header_lines = [
                f"🚚 <b>{i18n.get('staff.delivery.active_title', language)}</b>",
                i18n.get('staff.delivery.active_count', language, count=len(deliveries)),
            ]
            if location_status == 'missing':
                # Hard precondition not met — optimization is OFF until the
                # driver shares location. The list below is unsorted (just
                # in claim order) and there's no ETA. Be explicit so the
                # driver knows what to do.
                header_lines.append("")
                header_lines.append(
                    f"⚠️ <b>{i18n.get('staff.delivery.location_required_notice', language)}</b>"
                )
            elif location_status == 'stale':
                # We have a location but it's older than the freshness
                # threshold — the sequence is computed from the last
                # known position. Suggest a re-share for accuracy.
                header_lines.append("")
                header_lines.append(
                    f"ℹ️ {i18n.get('staff.delivery.location_stale_notice', language)}"
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
                lines = []
                # Only show the "Next stop · ETA · km" badge when we have a
                # real driver location to compute it from. When location is
                # missing/stale, the ETA would be measured from the depot /
                # city centre — confusing rather than useful, so we suppress it.
                if delivery.get('is_next') and location_status == 'fresh':
                    eta = delivery.get('eta_minutes_from_current_location')
                    km = delivery.get('distance_km_to_next')
                    next_parts = [
                        f"📍 <b>{i18n.get('staff.delivery.next_stop', language)}</b>"
                    ]
                    if eta is not None:
                        next_parts.append(
                            f"⏱ {i18n.get('staff.delivery.eta_minutes', language, minutes=int(eta))}"
                        )
                    if km is not None:
                        next_parts.append(
                            f"📏 {i18n.get('staff.delivery.distance_km', language, km=km)}"
                        )
                    lines.append(' · '.join(next_parts))

                lines.append(
                    format_active_delivery_summary(
                        delivery,
                        language,
                        include_money=True,
                        position=delivery.get('route_position'),
                    )
                )

                text = '\n'.join(lines)
                delivery_id = delivery.get('delivery_id') or delivery.get('id')

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"📋 {i18n.get('staff.delivery.manage', language)}",
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

            # Build detailed view — shared compact card (identical to the list card).
            status = delivery.get('status', '')
            text = format_active_delivery_summary(delivery, language, include_money=True)

            # Cache a snapshot for the status-change handlers, which read order
            # context solely from current_delivery. Store RAW values (the brief
            # formatter HTML-escapes on render).
            context.user_data['current_delivery'] = {
                'delivery_id': delivery_id,
                'order_id': delivery.get('order_id'),
                'order_number': delivery.get('order_number'),
                'customer_id': delivery.get('customer_id'),
                'customer_name': delivery.get('customer_name'),
                'customer_phone': delivery.get('customer_phone'),
                'status': status,
                'district': delivery.get('district'),
                'address': delivery.get('address'),
                'delivery_instructions': delivery.get('delivery_instructions'),
                'delivery_notes': delivery.get('delivery_notes'),
                'items': delivery.get('items', []),
                'total_amount': delivery.get('total_amount', 0),
                'payment_method': delivery.get('payment_method', ''),
                'payment_status': delivery.get('payment_status'),
                'amount_collected': delivery.get('amount_collected', 0),
                'outstanding_amount': delivery.get('outstanding_amount', 0),
                'cod_reserved_prepayment_amount': delivery.get('cod_reserved_prepayment_amount', 0),
                'expected_cash_to_collect': delivery.get('expected_cash_to_collect', 0),
                # Origin: driver's last known point
                'origin_lat': delivery.get('current_location_lat'),
                'origin_lng': delivery.get('current_location_lng'),
                # Destination: order address coordinates
                'destination_lat': delivery.get('destination_latitude'),
                'destination_lng': delivery.get('destination_longitude'),
                # Returnable bottles
                'expected_returnable_bottles': delivery.get('expected_returnable_bottles', 0),
                'customer_bottle_balance': delivery.get('customer_bottle_balance', 0),
            }

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
                    f"🗺 {i18n.get('staff.delivery.open_maps', language)}",
                    url=maps_url
                )
            ], [
                InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.back', language)}",
                    callback_data=f"staff_view_active_{delivery_info.get('delivery_id', 0)}"
                )
            ]])

            await query.edit_message_text(
                f"📍 {i18n.get('staff.delivery.navigate_text', language)}\n"
                f"{address}\n"
                f"({destination_lat}, {destination_lng})",
                reply_markup=keyboard,
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error navigating: {e}", exc_info=True)
            await self._handle_error(update, context)
