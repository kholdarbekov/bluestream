"""
Location Handler for Staff Bot.

Handles every Telegram location update from a delivery driver — both
one-shot location shares and live-location streams. Forwards the
coordinates to the backend's driver-level endpoint
(`POST /api/v1/staff/delivery/me/location`), which updates the driver's
profile location and enqueues a (debounced) route re-optimization off the
request thread. The response returned to us reflects the LAST persisted
sequence, not one computed from this share — the optimizer runs
asynchronously and, if it produces a new sequence, pushes a silent
`route-updated` webhook that refreshes any open "active deliveries" view.
We never poll or wait for that; the driver's next tap of "My active
deliveries" (a fresh `GET /delivery/active`) is what shows the update if
the webhook hasn't landed yet.

Important design notes:
  - Driver location is a **driver-level** state, not a per-delivery one.
    The legacy per-delivery endpoint required an in-progress delivery and
    silently dropped any location share before the first stop was picked up;
    that broke the route-optimization workflow which needs a start point
    *before* the first stop.
  - One-time location share is sufficient. We do not require Live Location.
  - Whenever we receive a location, we explicitly acknowledge to the driver
    so they know the share landed and the route was updated. Silently
    accepting a location is what created the bug we're fixing here.
"""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)


async def _run_optimize_after_location(update, context, language, token) -> bool:
    """Run the shared optimize-and-render path after a fallback location share.

    Module-level and late-importing on purpose: `active_delivery` imports the
    card renderer which imports this package, so a top-level import here would
    be circular. Tests patch this name.

    Leading underscore is load-bearing: `tests/unit/test_staff_handler_guards.py`
    treats any PUBLIC async function in `staff_bot/handlers/` whose first arg is
    `update` as a handler needing a role guard. This is an internal helper; the
    guard lives on `ActiveDeliveryHandler.run_optimize_and_render`.
    """
    from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler

    return await ActiveDeliveryHandler().run_optimize_and_render(
        update, context, language, token
    )


class LocationHandler(BaseHandler):
    """Forward driver location shares to the backend and ACK the driver."""

    @require_auth
    @require_delivery_driver
    async def handle_location_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle every incoming location: one-shot share *or* live-location
        stream. We unconditionally forward to the backend's driver-level
        endpoint and then send a brief confirmation message with a button
        to view the active deliveries list. The re-optimization the share
        triggers runs off-thread and debounced on the backend, so the list
        behind that button may still show the pre-share order for a moment;
        tapping it issues a fresh `GET /delivery/active` rather than reusing
        anything cached from this request.

        Live-location updates arrive as `update.edited_message.location` (the
        same message gets edited each time). For those we only ACK the very
        first update of the stream — repeatedly nagging the driver "✓
        location received" every 30 seconds while live location is on would
        be intolerable noise.
        """
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            return

        try:
            is_one_shot = bool(update.message and update.message.location)
            is_live_edit = bool(update.edited_message and update.edited_message.location)
            loc = (
                update.message.location if is_one_shot
                else update.edited_message.location if is_live_edit
                else None
            )
            if not loc:
                return

            async with api_client as client:
                response = await client.update_driver_location(
                    token,
                    loc.latitude,
                    loc.longitude,
                    horizontal_accuracy=getattr(loc, "horizontal_accuracy", None),
                )

            if not response.success:
                logger.warning(
                    "Driver location update rejected: status=%s error=%s code=%s",
                    response.status_code, response.error, response.error_code,
                )
                # Don't spam ACKs for live-location stream errors either.
                if is_one_shot:
                    msg = update.message
                    if response.error_code == "LOCATION_TOO_COARSE":
                        # Keep the armed flag: the driver asked to optimize and
                        # has not got it yet. Stepping outdoors and tapping the
                        # keyboard again finishes the original request.
                        button_text = i18n.get('staff.delivery.share_location_button', language)
                        await msg.reply_text(
                            f"📡 {i18n.get('staff.delivery.location_too_coarse', language)}",
                            reply_markup=CommonKeyboards.location_request(language, button_text),
                        )
                    else:
                        # Non-coarse failure: spec §4.2 says the armed flag
                        # clears here too. Leaving it armed would let an
                        # unrelated future pin silently fire an optimize the
                        # driver never got this turn. The location-request
                        # keyboard also collapsed the driver's menu, and this
                        # reply is the only thing that can bring it back.
                        context.user_data.pop('pending_optimize_after_location', None)
                        staff_roles = context.user_data.get('staff_roles', [])
                        await msg.reply_text(
                            f"⚠️ {i18n.get('staff.delivery.location_update_failed', language)}",
                            reply_markup=MenuKeyboards.main_menu(language, staff_roles),
                        )
                return

            logger.info(
                "Driver %s location updated via bot: lat=%s lng=%s (one_shot=%s live=%s)",
                update.effective_user.id, loc.latitude, loc.longitude, is_one_shot, is_live_edit,
            )

            # ACK only on the first location of a session — on a one-shot
            # share, or on the *first* edit of a live-location stream. We
            # use user_data to remember we've already ACKed for this stream.
            already_acked_live = context.user_data.get('live_location_ack_sent', False)

            # A one-shot share that we asked for because an optimize tap was
            # refused finishes that optimize here. Live-location edits never
            # do: a synchronous solve on every GPS tick would hammer OSRM.
            wants_optimize = is_one_shot and context.user_data.pop(
                'pending_optimize_after_location', False
            )

            if is_one_shot or (is_live_edit and not already_acked_live):
                staff_roles = context.user_data.get('staff_roles', [])
                msg = update.message or update.edited_message
                if msg is not None:
                    if wants_optimize:
                        # The route card is EDITED in place, and an edited
                        # message cannot carry a reply keyboard — so this
                        # message is the only thing that can bring the driver's
                        # main menu back after the location keyboard replaced
                        # it. Without it they are left holding a collapsed
                        # "Share location / Cancel" panel (route-UX plan
                        # 2026-08-11 Task 15).
                        await msg.reply_text(
                            f"✅ <b>{i18n.get('staff.delivery.location_received', language)}</b>",
                            reply_markup=MenuKeyboards.main_menu(language, staff_roles),
                            parse_mode='HTML',
                        )
                        await _run_optimize_after_location(update, context, language, token)
                    else:
                        ack_text = (
                            f"✅ <b>{i18n.get('staff.delivery.location_received', language)}</b>\n"
                            f"{i18n.get('staff.delivery.route_recalculated', language)}"
                        )
                        view_button = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                f"🚚 {i18n.get('staff.menu.active_deliveries', language)}",
                                callback_data="staff_active_deliveries",
                            )
                        ]])
                        # Re-attach the driver's persistent main-menu reply
                        # keyboard here instead of removing it. The
                        # location-request keyboard (CommonKeyboards.location_request)
                        # is `one_time_keyboard`, which only auto-hides
                        # *itself* on use — it does not bring back whatever
                        # keyboard was showing before it, so without this the
                        # driver's recallable keyboard stays the stale "Share
                        # Location / Cancel" one rather than the main menu
                        # (route-UX plan 2026-08-11 Task 15; recovery used to
                        # require discovering /menu).
                        await msg.reply_text(
                            ack_text,
                            reply_markup=MenuKeyboards.main_menu(language, staff_roles),
                            parse_mode='HTML',
                        )
                        await msg.reply_text(
                            f"🔍 {i18n.get('staff.delivery.tap_to_see_optimized', language)}",
                            reply_markup=view_button,
                        )
                if is_live_edit:
                    context.user_data['live_location_ack_sent'] = True
                elif is_one_shot:
                    # A one-shot share ends any live stream's ACK suppression;
                    # without this the first edit of the driver's NEXT live
                    # stream is silently un-ACKed (spec §4.4).
                    context.user_data.pop('live_location_ack_sent', None)

        except Exception as e:
            logger.error(f"Error handling location update: {e}", exc_info=True)
