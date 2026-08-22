"""
Manage Address Handler for Staff Bot
Allows operators to view and add delivery addresses for clients.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from shared.constants import is_within_tashkent
from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.keyboards.operator import OperatorKeyboards
from staff_bot.permissions import require_auth, require_operator
from staff_bot.i18n import i18n
from staff_bot.utils.formatters import escape_html

logger = logging.getLogger(__name__)

# Conversation states
ENTER_LABEL, ENTER_ADDRESS, ENTER_DISTRICT, ENTER_NOTES, CONFIRM_ADDRESS = range(40, 45)


class ManageAddressHandler(BaseHandler):
    """Handle client address management"""

    @require_auth
    @require_operator
    async def show_addresses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show client's addresses"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_op_addresses_{user_id}
            user_id = int(query.data.split('_')[-1])
            context.user_data['managing_addresses_for'] = user_id

            async with api_client as client:
                response = await client.get_user_addresses(token, user_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            addresses = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not addresses:
                text = i18n.get('staff.operator.no_addresses', language)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"➕ {i18n.get('staff.operator.add_address', language)}",
                        callback_data=f"staff_op_add_addr_{user_id}"
                    )],
                    [InlineKeyboardButton(
                        f"⬅️ {i18n.get('staff.back', language)}",
                        callback_data="staff_back_to_main"
                    )]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            lines = [f"📍 <b>{i18n.get('staff.operator.addresses_title', language)}</b>\n"]

            for addr in addresses:
                label = escape_html(addr.get('title', ''))
                address_line = escape_html(addr.get('full_address', ''))
                district = escape_html(addr.get('district', ''))

                lines.append(f"📍 <b>{label}</b>")
                if district:
                    lines.append(f"    {district}")
                if address_line:
                    lines.append(f"    {address_line}")
                lines.append("")

            text = '\n'.join(lines)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"➕ {i18n.get('staff.operator.add_address', language)}",
                    callback_data=f"staff_op_add_addr_{user_id}"
                )],
                [InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.back', language)}",
                    callback_data="staff_back_to_main"
                )]
            ])

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing addresses: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def start_add_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the add address conversation"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_op_add_addr_{user_id}
            user_id = int(query.data.split('_')[-1])
            context.user_data['adding_address_for'] = user_id
            context.user_data['new_address'] = {}

            text = i18n.get('staff.operator.enter_address_label', language)
            await query.edit_message_text(text, parse_mode='HTML')
            return ENTER_LABEL

        except Exception as e:
            logger.error(f"Error starting add address: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def receive_label(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive address label (Home, Office, etc.)"""
        language = await self._get_language(update, context)
        label = update.message.text.strip()

        if len(label) < 1 or len(label) > 100:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_label', language),
                parse_mode='HTML'
            )
            return ENTER_LABEL

        # Persist under 'title' — the key StaffService.add_client_address reads
        # (and the GET serializer exposes). Storing under 'label' made the backend
        # default every operator-created address to 'Home' and left the confirm
        # screen's title blank.
        context.user_data['new_address']['title'] = label

        await update.message.reply_text(
            i18n.get('staff.operator.enter_full_address', language),
            reply_markup=self._address_prompt_keyboard(language),
            parse_mode='HTML'
        )
        return ENTER_ADDRESS

    @staticmethod
    def _address_prompt_keyboard(language: str):
        """The pin half of the address step.

        `request_location` is a field of `KeyboardButton` and of nothing else,
        so the pin option cannot be offered from the inline keyboards the rest
        of this flow draws — without this reply keyboard "or attach a pin" is a
        sentence the operator has no button for.

        No Cancel row: on THIS step a Cancel tap arrives as plain text and the
        state's text handler would try to geocode the word "Cancel" as a street.
        The ways out stay the main-menu escape (`_conv_menu_escape`) and
        /cancel, both of which already end the conversation.
        """
        return CommonKeyboards.location_request(
            language,
            i18n.get('staff.operator.share_location', language),
            include_cancel=False,
        )

    def _main_menu_keyboard(self, context: ContextTypes.DEFAULT_TYPE, language: str):
        """The operator's main menu, to put back once the address step is done.

        `_address_prompt_keyboard` REPLACES the main reply keyboard while that
        step is open. Leaving it up would hide every menu label the
        conversation's own escape hatch matches on, so the operator's only exit
        from a half-entered address would be /cancel.
        """
        return MenuKeyboards.main_menu(language, context.user_data.get('staff_roles', []))

    async def _geocode(self, token: str, address: str):
        """Place `address` on the map; return (latitude, longitude) or (None, None).

        (None, None) means the geocoder could not place it. The caller refuses
        rather than saving anyway: an address stored with no coordinates is one
        the delivery-zone SSOT can never speak about again, at this write or at
        any later edit of the row.

        The route itself belongs to `StaffAPIClient.geocode_address`, which is
        the same backend endpoint the CUSTOMER bot calls
        (`telegram_bot/api_client.geocode_address`) — so the two bots resolve an
        address identically and nothing here has to know where that lives.
        """
        async with api_client as client:
            response = await client.geocode_address(token, address)

        payload = response.data if response.success and isinstance(response.data, dict) else {}
        latitude = payload.get('latitude')
        longitude = payload.get('longitude')
        if latitude is None or longitude is None:
            logger.info("Operator address could not be geocoded: %r", address)
            return None, None

        return latitude, longitude

    @require_auth
    @require_operator
    async def receive_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive the typed address line and resolve it to a delivery-zone pin.

        THIS IS WHERE THE DELIVERY-ZONE SSOT REACHES THIS FLOW.
        `shared.constants.TASHKENT_POLYGON` is the SSOT and
        `ensure_within_delivery_zone` the guard every coordinate-bearing write
        funnels through — but that guard is a documented NO-OP when a coordinate
        is missing, and this flow used to collect four lines of free text and no
        pin at all. "Samarqand, Registon ko'chasi 5" was therefore a perfectly
        acceptable delivery address and the driver found out 280 km later.

        So the typed line is geocoded server-side through the same backend route
        the customer bot uses, and an address that cannot be placed inside the
        polygon never reaches the write.
        """
        language = await self._get_language(update, context)
        address = update.message.text.strip()

        if len(address) < 5:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_address', language),
                reply_markup=self._address_prompt_keyboard(language),
                parse_mode='HTML'
            )
            return ENTER_ADDRESS

        new_address = context.user_data.setdefault('new_address', {})

        # A pin shared at this step outranks the geocoder: it is exact where a
        # geocoder's answer is a guess, and `receive_location` has already
        # zone-checked it. Typing the street line after a pin is the documented
        # recovery when reverse geocoding could not name the place.
        if new_address.get('latitude') is None:
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return ConversationHandler.END

            latitude, longitude = await self._geocode(token, address)

            if latitude is None:
                await update.message.reply_text(
                    i18n.get('staff.operator.address_not_found', language),
                    reply_markup=self._address_prompt_keyboard(language),
                    parse_mode='HTML'
                )
                return ENTER_ADDRESS

            if not is_within_tashkent(latitude, longitude):
                logger.info(
                    "Operator address refused as out of zone: %r -> %s, %s",
                    address, latitude, longitude,
                )
                await update.message.reply_text(
                    i18n.get('staff.operator.outside_delivery_area', language),
                    reply_markup=self._address_prompt_keyboard(language),
                    parse_mode='HTML'
                )
                return ENTER_ADDRESS

            new_address['latitude'] = latitude
            new_address['longitude'] = longitude

        # The operator's own line is what the driver reads at the door, so it is
        # what gets stored. Only the pin comes from the geocoder — a geocoder's
        # `formatted_address` is coarser and would drop the floor and flat the
        # caller just dictated.
        new_address['full_address'] = address

        await update.message.reply_text(
            i18n.get('staff.operator.enter_district', language),
            reply_markup=self._main_menu_keyboard(context, language),
            parse_mode='HTML'
        )
        return ENTER_DISTRICT

    @require_auth
    @require_operator
    async def receive_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive a shared pin instead of a typed address line.

        The other half of the same choice the customer bot offers
        (`telegram_bot/handlers/profile.location_received`): a pin carries exact
        coordinates, so the zone guard runs on the real point rather than on a
        geocoder's reading of a sentence.

        Reads through `effective_message` rather than `update.message`, the same
        way `bot._conv_menu_escape` does and for the same reason: a LIVE
        location arrives as an `edited_message`, where `update.message` is None
        and every attribute access on it is an AttributeError.
        """
        language = await self._get_language(update, context)
        message = update.effective_message
        location = message.location if message else None

        if not location:
            # Unreachable through the registered `filters.LOCATION` handler, but
            # this method is a public entry point and a missing pin must not
            # crash the step the operator is standing in.
            if message:
                await message.reply_text(
                    i18n.get('staff.operator.invalid_address', language),
                    reply_markup=self._address_prompt_keyboard(language),
                    parse_mode='HTML'
                )
            return ENTER_ADDRESS

        if not is_within_tashkent(location.latitude, location.longitude):
            logger.info(
                "Operator pin refused as out of zone: %s, %s",
                location.latitude, location.longitude,
            )
            await message.reply_text(
                i18n.get('staff.operator.outside_delivery_area', language),
                reply_markup=self._address_prompt_keyboard(language),
                parse_mode='HTML'
            )
            return ENTER_ADDRESS

        new_address = context.user_data.setdefault('new_address', {})
        new_address['latitude'] = location.latitude
        new_address['longitude'] = location.longitude

        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        async with api_client as client:
            response = await client.reverse_geocode_address(
                token, location.latitude, location.longitude
            )

        payload = response.data if response.success and isinstance(response.data, dict) else {}
        formatted_address = payload.get('formatted_address')

        if not formatted_address:
            # The pin is KEPT — it is already zone-checked — and the operator
            # supplies the street line the driver reads at the door.
            # `receive_address` sees the stored latitude and skips geocoding, so
            # this is a recovery and not a dead end when the geocoder is down.
            await message.reply_text(
                i18n.get('staff.operator.location_needs_address', language),
                parse_mode='HTML'
            )
            return ENTER_ADDRESS

        new_address['full_address'] = formatted_address

        # Read back what the pin resolved to: an operator on a call can catch
        # "that is MY street" here, which is the one mistake a pin invites.
        await message.reply_text(
            i18n.get(
                'staff.operator.location_received',
                language,
                address=escape_html(formatted_address),
            ),
            reply_markup=self._main_menu_keyboard(context, language),
            parse_mode='HTML'
        )
        await message.reply_text(
            i18n.get('staff.operator.enter_district', language),
            parse_mode='HTML'
        )
        return ENTER_DISTRICT

    @require_auth
    @require_operator
    async def receive_district(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive district"""
        language = await self._get_language(update, context)
        district = update.message.text.strip()

        if district.lower() in ('-', 'skip', 'пропустить', "o'tkazish"):
            context.user_data['new_address']['district'] = None
        else:
            context.user_data['new_address']['district'] = district

        await update.message.reply_text(
            i18n.get('staff.operator.enter_delivery_notes', language),
            parse_mode='HTML'
        )
        return ENTER_NOTES

    @require_auth
    @require_operator
    async def receive_address_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive delivery notes for address"""
        language = await self._get_language(update, context)
        notes = update.message.text.strip()

        if notes.lower() in ('-', 'skip', 'пропустить', "o'tkazish"):
            context.user_data['new_address']['delivery_notes'] = None
        else:
            context.user_data['new_address']['delivery_notes'] = notes

        # Show confirmation
        addr = context.user_data['new_address']
        lines = [
            f"📍 <b>{i18n.get('staff.operator.confirm_address', language)}</b>\n",
            f"🏷 {escape_html(addr.get('title', ''))}",
            f"📍 {escape_html(addr.get('full_address', ''))}",
        ]
        if addr.get('district'):
            lines.append(f"🏘 {escape_html(addr['district'])}")
        if addr.get('delivery_notes'):
            lines.append(f"💬 {escape_html(addr['delivery_notes'])}")

        text = '\n'.join(lines)
        keyboard = CommonKeyboards.confirm_cancel(
            language,
            confirm_data="staff_op_confirm_address",
            cancel_data="staff_back_to_main"
        )

        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        return CONFIRM_ADDRESS

    @require_auth
    @require_operator
    async def confirm_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and save the address"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            user_id = context.user_data.get('adding_address_for')
            addr_data = context.user_data.get('new_address', {})

            if not user_id:
                await self._handle_error(update, context)
                return ConversationHandler.END

            async with api_client as client:
                response = await client.add_client_address(token, user_id, addr_data)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            active_order = context.user_data.get('new_order') or {}
            if active_order.get('client_id') == user_id:
                # Continue operator order flow: return directly to address selection.
                list_response = await client.get_user_addresses(token, user_id)
                if list_response.success:
                    addresses = (
                        list_response.data
                        if isinstance(list_response.data, list)
                        else list_response.data.get('items', [])
                    )
                    await query.edit_message_text(
                        i18n.get('staff.operator.select_address', language),
                        reply_markup=OperatorKeyboards.address_list(language, addresses, user_id),
                        parse_mode='HTML'
                    )
                else:
                    await query.edit_message_text(
                        f"✅ {i18n.get('staff.operator.address_saved', language)}",
                        reply_markup=CommonKeyboards.back_button(language),
                        parse_mode='HTML'
                    )
            else:
                await query.edit_message_text(
                    f"✅ {i18n.get('staff.operator.address_saved', language)}",
                    reply_markup=CommonKeyboards.back_button(language),
                    parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error saving address: {e}", exc_info=True)
            await self._handle_error(update, context)

        context.user_data.pop('new_address', None)
        context.user_data.pop('adding_address_for', None)
        return ConversationHandler.END

    @require_auth
    @require_operator
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address creation"""
        context.user_data.pop('new_address', None)
        context.user_data.pop('adding_address_for', None)
        language = await self._get_language(update, context)

        text = i18n.get('staff.cancelled', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=CommonKeyboards.back_button(language)
            )
        else:
            # /cancel is the documented exit while `_address_prompt_keyboard`
            # has replaced the main menu, so this branch has to put the menu
            # back; the callback branch above cannot (an edit cannot change a
            # reply keyboard) and does not need to — by the time an inline
            # Cancel is on screen the district prompt has already restored it.
            await update.message.reply_text(
                text, reply_markup=CommonKeyboards.back_button(language)
            )
            await update.message.reply_text(
                i18n.get('staff.menu.title', language),
                reply_markup=self._main_menu_keyboard(context, language)
            )
        return ConversationHandler.END
