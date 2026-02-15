"""
Live Location Handler for Staff Bot
Handles sharing and updating delivery person's live location.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.common import CommonKeyboards
from permissions import require_auth, require_delivery_driver
from i18n import i18n

logger = logging.getLogger(__name__)


class LocationHandler(BaseHandler):
    """Handle live location sharing for deliveries"""

    @require_auth
    @require_delivery_driver
    async def handle_location_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle incoming location updates from the delivery person.
        This is triggered when the user shares their live location or sends a location.
        """
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            return

        try:
            location = update.message.location if update.message else None
            edited_location = update.edited_message.location if update.edited_message else None

            loc = location or edited_location
            if not loc:
                return

            # Get current active delivery from context
            delivery_info = context.user_data.get('current_delivery', {})
            delivery_id = delivery_info.get('delivery_id')

            if not delivery_id:
                # Try to find an active delivery to update
                async with api_client as client:
                    response = await client.get_active_deliveries(token)

                if response.success:
                    deliveries = response.data if isinstance(response.data, list) else response.data.get('items', [])
                    # Update location for the first in_transit delivery
                    for d in deliveries:
                        if d.get('status') in ('in_transit', 'arrived', 'picked_up'):
                            delivery_id = d.get('delivery_id') or d.get('id')
                            break

            if not delivery_id:
                return  # No active delivery to update

            async with api_client as client:
                await client.update_location(
                    token, delivery_id, loc.latitude, loc.longitude
                )

            logger.debug(
                f"Location updated for delivery {delivery_id}: "
                f"{loc.latitude}, {loc.longitude}"
            )

        except Exception as e:
            logger.error(f"Error handling location update: {e}", exc_info=True)

    @require_auth
    @require_delivery_driver
    async def prompt_share_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the user to share their live location"""
        query = update.callback_query
        if query:
            await query.answer()
        language = await self._get_language(update, context)

        try:
            text = i18n.get('staff.delivery.share_location_prompt', language)

            keyboard = CommonKeyboards.back_button(
                language,
                callback_data=f"staff_view_active_{context.user_data.get('current_delivery', {}).get('delivery_id', 0)}"
            )

            if query:
                await query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error prompting location share: {e}", exc_info=True)
            await self._handle_error(update, context)
