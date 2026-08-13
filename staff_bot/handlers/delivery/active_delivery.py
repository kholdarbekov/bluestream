"""
Active Delivery Handler for Staff Bot
Shows and manages deliveries currently assigned to the delivery person.
"""
import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.delivery import route_card
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


class ActiveDeliveryHandler(BaseHandler):
    """Handle active delivery listing and management"""

    @staticmethod
    def _compute_render_signature(text: str, keyboard) -> str:
        """Delegates to the SSOT in staff_bot.utils.render_signature."""
        from staff_bot.utils.render_signature import compute_render_signature

        return compute_render_signature(text, keyboard)

    @require_auth
    @require_delivery_driver
    async def show_active_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Render the driver's ROUTE CARD (route-UX Phase 3, spec §6).

        ONE long-lived message per driver per shift, edited in place —
        editMessageText produces no notification, which is what makes a chat
        surface usable as a driver app. The old header+N-cards render
        (delete all, resend all) is gone; every historical entry point
        (callback, menu tap, back-buttons, webhook alert button) lands here
        unchanged and funnels into route_card.render_route_card."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        if update.callback_query:
            # Guarded, not bare: `optimize_routes` calls this method AFTER
            # already answering the same callback_query itself (review
            # round 1, I1) -- a bare second `.answer()` can raise (and take
            # down the re-render with it) or silently swallow the caller's
            # own alert. `_safe_callback_answer` is the repo's one guard
            # for exactly this, so route through it instead of a new guard.
            await self._safe_callback_answer(update.callback_query, "", show_alert=False)
        await self._render_card_from_update(update, context, language, token)

    async def _render_card_from_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
        token: str,
        view: Optional[str] = None,
    ):
        """Fetch the active payload and hand it to the card renderer.

        The tap's source message id is the repost-heuristic reference: a tap
        far below the card means the card is buried and gets reposted
        (spec §6.3). Error handling mirrors the retired list renderer."""
        try:
            async with api_client as client:
                response = await client.get_active_deliveries(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            payload = (
                response.data if isinstance(response.data, dict)
                else {"items": response.data or []}
            )
            src = update.callback_query.message if update.callback_query else update.message
            chat_id = src.chat.id if src is not None and src.chat else update.effective_user.id
            reference_message_id = src.message_id if src is not None else None

            # Last-resort, in-session-only stand-in for Redis state (review
            # round 1, I2): bounds a Redis outage to "one send per bot
            # restart" instead of "one new pinned card per tap". See
            # route_card.render_route_card's session_hint paragraph.
            session_hint = context.user_data.setdefault('route_card_session', {})

            await route_card.render_route_card(
                context.bot,
                telegram_id=update.effective_user.id,
                chat_id=chat_id,
                language=language,
                payload=payload,
                view=view,
                reference_message_id=reference_message_id,
                session_hint=session_hint,
            )
        except Exception as e:
            logger.error(f"Error rendering route card: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def switch_route_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Flip the card between next-stop and all-stops (same message)."""
        query = update.callback_query
        await query.answer()
        view = query.data.rsplit('_', 1)[-1]  # 'next' | 'all' (pattern-guarded)
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        await self._render_card_from_update(update, context, language, token, view=view)

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

            # The card message is about to morph into the stop detail (and
            # possibly the at-door flows). Freeze webhook-driven silent card
            # edits until the next full card render un-borrows it -- an edit
            # here would yank the driver's screen mid-collection (Phase 3
            # Task 7). The current_delivery snapshot below is UNCHANGED.
            from staff_bot.utils import route_card_state
            await route_card_state.mark_borrowed(update.effective_user.id)
            # Redis-outage backstop (Task 7 fix round 1): mark_borrowed above
            # is a no-op when Redis is down (it starts with a `load` that
            # returns None), so the borrow would never land and the SAME
            # stranding bug survives an outage via Task 6's session_hint
            # fallback (route_card.render_route_card, `state is None and
            # session_hint` branch) -- that fallback replays whatever view
            # is still sitting in the hint, which without this line stays
            # "next" forever. Mirror the borrow into the hint too, same
            # field Task 6 already reads. This can never let the hint WIN
            # over a healthy Redis: render_route_card only ever consults
            # session_hint when `state is None`, so this write is inert
            # whenever Redis is up (a real Redis `state` is loaded first and
            # used instead). No-op when no card was ever rendered into this
            # session yet -- nothing to borrow.
            session_hint = context.user_data.get('route_card_session')
            if session_hint:
                session_hint['view'] = route_card_state.VIEW_BORROWED

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
                'apartment_number': delivery.get('apartment_number'),
                'floor_number': delivery.get('floor_number'),
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
                # Destination: order address coordinates
                'destination_lat': delivery.get('destination_latitude'),
                'destination_lng': delivery.get('destination_longitude'),
                # Returnable bottles
                'expected_returnable_bottles': delivery.get('expected_returnable_bottles', 0),
                'customer_bottle_balance': delivery.get('customer_bottle_balance', 0),
                # SIGNED place balance — the clamped anchor above can never go
                # below 0, so this is the only way the at-door prompt can tell
                # "over-returned" from "no empties on record". This snapshot is
                # an explicit whitelist (see the note below): omit the key and
                # the over-returned prompt silently never fires.
                'place_bottle_balance_signed': delivery.get('place_bottle_balance_signed', 0),
                # Place-group COD context (spec 8). This snapshot WHITELISTS
                # keys, so the at-door cash prompt — which reads only
                # current_delivery — sees the place block just when it's copied
                # here. Zeros/False for ungrouped addresses.
                'is_place_grouped': delivery.get('is_place_grouped', False),
                'place_group_id': delivery.get('place_group_id'),
                'place_group_label': delivery.get('place_group_label'),
                'place_outstanding_cod_total': delivery.get('place_outstanding_cod_total', 0),
                'place_active_cod_debt_count': delivery.get('place_active_cod_debt_count', 0),
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

            # Dispatch has locked this route, so the backend deliberately did
            # nothing. Say so: an unchanged list after a deliberate tap
            # otherwise reads as the button being broken.
            if (response.data or {}).get("route_locked"):
                await query.answer(
                    i18n.get('staff.route.locked_by_dispatch', language),
                    show_alert=True,
                )
                await self.show_active_deliveries(update, context)
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

            if destination_lat is None or destination_lng is None:
                await query.answer(
                    i18n.get('staff.delivery.no_address', language),
                    show_alert=True
                )
                return

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
