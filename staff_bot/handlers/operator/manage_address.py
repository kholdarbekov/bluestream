"""
Manage Address Handler for Staff Bot
Allows operators to view and add delivery addresses for clients.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
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
            parse_mode='HTML'
        )
        return ENTER_ADDRESS

    @require_auth
    @require_operator
    async def receive_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive full address text"""
        # NOTE (delivery-zone SSOT): this operator flow collects a free-text address
        # with no GPS coordinates, so it cannot be polygon-validated here. Coordinate-
        # bearing paths are enforced via shared.constants.is_within_tashkent + the
        # backend UserAddress backstop. Enforcing zone on text addresses requires
        # server-side geocoding and is tracked as a follow-up (see docs).
        language = await self._get_language(update, context)
        address = update.message.text.strip()

        if len(address) < 5:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_address', language),
                parse_mode='HTML'
            )
            return ENTER_ADDRESS

        context.user_data['new_address']['full_address'] = address

        await update.message.reply_text(
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
            await update.message.reply_text(
                text, reply_markup=CommonKeyboards.back_button(language)
            )
        return ConversationHandler.END
