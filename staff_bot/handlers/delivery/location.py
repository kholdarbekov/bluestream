"""
Location Handler for Staff Bot.

Handles every Telegram location update from a delivery driver — both
one-shot location shares and live-location streams. Forwards the
coordinates to the backend's driver-level endpoint
(`POST /api/v1/staff/delivery/me/location`), which updates the driver's
profile location and re-runs route optimization in one round-trip.

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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)


class LocationHandler(BaseHandler):
    """Forward driver location shares to the backend and ACK the driver."""

    @require_auth
    @require_delivery_driver
    async def handle_location_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle every incoming location: one-shot share *or* live-location
        stream. We unconditionally forward to the backend's driver-level
        endpoint and then send a brief confirmation message with a button
        to view the freshly optimized active deliveries list.

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
                    token, loc.latitude, loc.longitude
                )

            if not response.success:
                logger.warning(
                    "Driver location update rejected: status=%s error=%s code=%s",
                    response.status_code, response.error, response.error_code,
                )
                # Don't spam ACKs for live-location stream errors either.
                if is_one_shot:
                    msg = update.message
                    await msg.reply_text(
                        f"⚠️ {i18n.get('staff.delivery.location_update_failed', language)}",
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

            if is_one_shot or (is_live_edit and not already_acked_live):
                ack_text = (
                    f"✅ <b>{i18n.get('staff.delivery.location_received', language)}</b>\n"
                    f"{i18n.get('staff.delivery.route_recalculated', language)}"
                )
                view_button = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"\U0001f69a {i18n.get('staff.menu.active_deliveries', language)}",
                        callback_data="staff_active_deliveries",
                    )
                ]])
                # Drop the "Share location" reply keyboard now that we have it.
                msg = update.message or update.edited_message
                if msg is not None:
                    await msg.reply_text(
                        ack_text,
                        reply_markup=ReplyKeyboardRemove(),
                        parse_mode='HTML',
                    )
                    await msg.reply_text(
                        f"\U0001f50d {i18n.get('staff.delivery.tap_to_see_optimized', language)}",
                        reply_markup=view_button,
                    )
                if is_live_edit:
                    context.user_data['live_location_ack_sent'] = True

        except Exception as e:
            logger.error(f"Error handling location update: {e}", exc_info=True)
