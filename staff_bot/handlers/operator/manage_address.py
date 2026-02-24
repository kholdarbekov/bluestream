"""
Manage Address Handler for Staff Bot
Allows operators to view and add delivery addresses for clients.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.common import CommonKeyboards
from permissions import require_auth, require_operator
from i18n import i18n

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
                        f"\u2795 {i18n.get('staff.operator.add_address', language)}",
                        callback_data=f"staff_op_add_addr_{user_id}"
                    )],
                    [InlineKeyboardButton(
                        f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                        callback_data="staff_back_to_main"
                    )]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            lines = [f"\U0001f4cd <b>{i18n.get('staff.operator.addresses_title', language)}</b>\n"]

            for addr in addresses:
                label = addr.get('label', '')
                address_line = addr.get('address_line_1', '')
                district = addr.get('district', '')

                lines.append(f"\U0001f4cd <b>{label}</b>")
                if district:
                    lines.append(f"    {district}")
                if address_line:
                    lines.append(f"    {address_line}")
                lines.append("")

            text = '\n'.join(lines)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"\u2795 {i18n.get('staff.operator.add_address', language)}",
                    callback_data=f"staff_op_add_addr_{user_id}"
                )],
                [InlineKeyboardButton(
                    f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
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

        context.user_data['new_address']['label'] = label

        await update.message.reply_text(
            i18n.get('staff.operator.enter_full_address', language),
            parse_mode='HTML'
        )
        return ENTER_ADDRESS

    async def receive_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive full address text"""
        language = await self._get_language(update, context)
        address = update.message.text.strip()

        if len(address) < 5:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_address', language),
                parse_mode='HTML'
            )
            return ENTER_ADDRESS

        context.user_data['new_address']['address_line_1'] = address

        await update.message.reply_text(
            i18n.get('staff.operator.enter_district', language),
            parse_mode='HTML'
        )
        return ENTER_DISTRICT

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
            f"\U0001f4cd <b>{i18n.get('staff.operator.confirm_address', language)}</b>\n",
            f"\U0001f3f7 {addr.get('label', '')}",
            f"\U0001f4cd {addr.get('address_line_1', '')}",
        ]
        if addr.get('district'):
            lines.append(f"\U0001f3d8 {addr['district']}")
        if addr.get('delivery_notes'):
            lines.append(f"\U0001f4ac {addr['delivery_notes']}")

        text = '\n'.join(lines)
        keyboard = CommonKeyboards.confirm_cancel(
            language,
            confirm_data="staff_op_confirm_address",
            cancel_data="staff_back_to_main"
        )

        await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        return CONFIRM_ADDRESS

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

            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.operator.address_saved', language)}",
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error saving address: {e}", exc_info=True)
            await self._handle_error(update, context)

        context.user_data.pop('new_address', None)
        context.user_data.pop('adding_address_for', None)
        return ConversationHandler.END

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
