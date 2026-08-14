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
from staff_bot.utils import route_card_state
from staff_bot.utils.formatters import (
    escape_html,
    format_active_delivery_summary,
    format_local_time,
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
            #
            # This toast is a real one now, not the empty string that only
            # stopped the spinner (task-5). When this call is the SECOND
            # answer of the same callback_query (the `optimize_routes` ->
            # `show_active_deliveries` path), Telegram itself rejects a
            # second `answerCallbackQuery` regardless of its content --
            # `_safe_callback_answer` swallows that rejection exactly as it
            # always has, so the caller's own alert (already delivered) is
            # never clobbered. See TestDoubleAnswerGuard in
            # tests/staff_bot/test_route_card_entrypoints.py.
            await self._safe_callback_answer(
                update.callback_query,
                i18n.get('staff.route.refreshed_toast', language,
                         time=format_local_time(with_seconds=True)),
                show_alert=False,
            )
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

            outcome = await route_card.render_route_card(
                context.bot,
                telegram_id=update.effective_user.id,
                chat_id=chat_id,
                language=language,
                payload=payload,
                view=view,
                reference_message_id=reference_message_id,
                session_hint=session_hint,
                # Every caller of this method is a DRIVER ACTION (menu tap,
                # inline button, view switch, post-optimize re-render). The
                # webhook path never comes through here -- it calls
                # route_card.update_card_for_driver directly.
                force=True,
            )
            if outcome == route_card.RenderOutcome.FAILED:
                await self._send_card_fallback(update, language, payload, view)
        except Exception as e:
            logger.error(f"Error rendering route card: {e}", exc_info=True)
            await self._handle_error(update, context)

    async def _send_card_fallback(self, update, language, payload, view):
        """The card could not be rendered and the driver asked for it.

        One plain silent message carrying the same content -- no pin, no
        state write, so it can never become a second tracked card. This
        deliberately spends a send that the card's own budget forbids,
        because the alternative is what the driver reported: flood control,
        a network blip and 'nothing changed' all looking identical, i.e.
        a bot that appears dead (tap-feedback spec §4.4).

        Deliberately INERT: text only, no inline keyboard. An earlier
        revision passed the card's real keyboard, which made this untracked
        message a fully functional second card -- tapping "Start this stop"
        on it calls `mark_borrowed` on the REAL pinned card (freezing every
        webhook refresh of it) and then edits THIS message into the at-door
        surface, orphaning it as a stale detail view with live buttons.
        Actions belong on the one tracked card; this is a read-only "here is
        what the card would have said".

        Honest limitation: the first cause listed above is flood control,
        and a sendMessage issued right after a rate-limited edit in the same
        chat will often be rate-limited too (staff_bot installs no
        AIORateLimiter, so there is no backoff). This is a best-effort last
        resort, not a guarantee.
        """
        message = update.effective_message
        if message is None:
            return
        text, _ = route_card.build_view(
            payload, language, view or route_card_state.VIEW_NEXT, with_seconds=True
        )
        try:
            await message.reply_text(
                text, parse_mode="HTML",
                disable_notification=True,
            )
        except Exception as exc:  # noqa: BLE001 -- last resort; nothing left to try
            logger.warning("route card fallback send failed: %s", exc)

    @require_auth
    @require_delivery_driver
    async def refresh_route_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """🔄 on the card.

        Answers the callback FIRST, then renders. Callback ids expire in
        ~10-15 seconds and commit 15a0501 already fixed one case of a slow
        handler eating them -- the acknowledgement must not be hostage to
        the backend round trip behind it. That ordering is also why the
        toast carries only a timestamp and not a stop count: the count is
        not known until after the fetch.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        await self._safe_callback_answer(
            query,
            i18n.get('staff.route.refreshed_toast', language,
                     time=format_local_time(with_seconds=True)),
            show_alert=False,
        )
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        await self._render_card_from_update(update, context, language, token)

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
        """Driver tapped 'Optimize route' on the card.

        The backend answers from the driver's stored position, so this is a
        single tap whenever that position is still fresh. When it is stale or
        absent the backend refuses and `run_optimize_and_render` asks for a
        location once — the pin that follows finishes the optimization itself
        (see LocationHandler), so the driver never taps this button twice.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await query.answer()
            await self._handle_auth_error(update, language)
            return

        await self.run_optimize_and_render(update, context, language, token)

    @require_auth
    @require_delivery_driver
    async def run_optimize_and_render(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
        token: str,
    ) -> Optional[bool]:
        """Run optimization and show the result.

        Three-valued return, and the distinction is load-bearing for callers
        that check `is False` rather than plain truthiness:
        - `True` — ran, and the optimization (or a route-locked alert) landed.
        - `False` — ran, but did not optimize for a business reason (412
          LOCATION_REQUIRED, or another API failure).
        - `None` — did NOT run: `@require_auth`/`@require_delivery_driver`
          rejected the caller before the body ever executed (their early-return
          paths at `staff_bot/permissions.py:73` and `:115` are bare `return`,
          i.e. `None`). Distinct from `False` on purpose — a rejected caller
          is not the same event as "ran and declined".

        The ONE implementation of "optimize, handle a dispatch lock, render the
        card". Called by the card button and, after a fallback location share,
        by LocationHandler — a second copy would let the two paths disagree
        about what a locked route or a 412 means.

        `update` may have no `callback_query` (the post-location path), so every
        callback interaction here goes through `_safe_callback_answer` or is
        guarded.

        Carries the guard decorators even though its only callers are already
        guarded: `tests/unit/test_staff_handler_guards.py` AST-walks
        `staff_bot/handlers/` and treats any public async function whose first
        arg is `self`/`update` as a handler. The role re-check is also genuinely
        wanted on the location path, where the update did not come from the
        button that was authorised a moment ago — the `None` branch above is
        reachable there, not theoretical.
        """
        query = update.callback_query

        try:
            async with api_client as client:
                response = await client.optimize_route(token)

            # Driver location precondition not met — prompt to share rather
            # than silently optimizing from a fallback origin. Arm the flag so
            # the incoming pin re-runs this method instead of just ACKing.
            if (
                not response.success
                and response.status_code == 412
                and (response.error_code == "LOCATION_REQUIRED" or "LOCATION" in (response.error or "").upper())
            ):
                if query is not None:
                    await self._safe_callback_answer(
                        query,
                        i18n.get('staff.delivery.share_location_first_toast', language),
                        show_alert=True,
                    )
                context.user_data['pending_optimize_after_location'] = True
                prompt = i18n.get('staff.delivery.share_location_prompt', language)
                button_text = i18n.get('staff.delivery.share_location_button', language)
                target = query.message if query is not None else update.message
                await target.reply_text(
                    prompt,
                    # include_cancel=False: Cancel has no handler on this path,
                    # so it was an escape that escaped nothing. The driver's
                    # real exit is the route card's own inline buttons, which
                    # this reply keyboard does not cover.
                    reply_markup=CommonKeyboards.location_request(
                        language, button_text, include_cancel=False
                    ),
                )
                return False

            if not response.success:
                if query is not None:
                    await self._safe_callback_answer(query, "", show_alert=False)
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return False

            # Dispatch has locked this route, so the backend deliberately did
            # nothing. Say so: an unchanged list after a deliberate tap
            # otherwise reads as the button being broken.
            if (response.data or {}).get("route_locked"):
                if query is not None:
                    await self._safe_callback_answer(
                        query,
                        i18n.get('staff.route.locked_by_dispatch', language),
                        show_alert=True,
                    )
                await self.show_active_deliveries(update, context)
                return True

            # Optimization ran successfully — confirm and re-render.
            if query is not None:
                await self._safe_callback_answer(
                    query,
                    i18n.get('staff.delivery.route_updated_toast', language),
                    show_alert=False,
                )
            await self.show_active_deliveries(update, context)
            return True
        except Exception as e:
            logger.error(f"Error in run_optimize_and_render: {e}", exc_info=True)
            await self._handle_error(update, context)
            return False

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
