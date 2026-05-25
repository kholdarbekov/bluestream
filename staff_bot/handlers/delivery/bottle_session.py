"""Co-driver session join/leave flow for delivery drivers.

Allows a driver to join another driver's open bottle session (e.g. two drivers
sharing the same truck). The handler provides:
  - List of joinable sessions
  - Join confirmation
  - Leave session
  - Current membership status display
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver

logger = logging.getLogger(__name__)

# Conversation state (used when join flow is a ConversationHandler)
JOIN_SESSION_CONFIRM = 200


class BottleSessionMembershipHandler(BaseHandler):
    """Handle co-driver session join/leave interactions."""

    @require_auth
    @require_delivery_driver
    async def show_joinable_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of open sessions the driver can join."""
        query = update.callback_query
        if query:
            await query.answer()

        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_joinable_bottle_sessions(token)

            if not response.success:
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                text = f"❌ {error_msg}"
                keyboard = CommonKeyboards.back_button(language, "staff_back_to_main")
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            sessions = response.data or []

            if not sessions:
                text = i18n.get('staff.bottles.no_open_sessions', language)
                keyboard = CommonKeyboards.back_button(language, "staff_back_to_main")
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard)
                else:
                    await update.message.reply_text(text, reply_markup=keyboard)
                return

            # Build session list buttons
            buttons = []
            for s in sessions:
                owner_name = s.get('owner_name') or 'Driver'
                inventory = s.get('current_inventory', 0)
                loaded = s.get('bottles_loaded', 0)
                label = f"📦 {owner_name} — {inventory}/{loaded}"
                buttons.append([
                    InlineKeyboardButton(label, callback_data=f"bottles_join_confirm_{s['session_id']}")
                ])
            buttons.append([
                InlineKeyboardButton(
                    i18n.get('common.back', language),
                    callback_data='staff_back_to_main',
                )
            ])

            text = f"<b>{i18n.get('staff.bottles.choose_session_to_join', language)}</b>"
            keyboard = InlineKeyboardMarkup(buttons)
            if query:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing joinable sessions: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_join_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show confirmation prompt before joining a session."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Callback: bottles_join_confirm_{session_id}
            session_id = int(query.data.split('_')[-1])
            context.user_data['pending_join_session_id'] = session_id

            # Fetch joinable list again to show session details
            async with api_client as client:
                response = await client.get_joinable_bottle_sessions(token)

            session_info = None
            if response.success and response.data:
                session_info = next(
                    (s for s in response.data if s['session_id'] == session_id), None
                )

            if not session_info:
                await query.edit_message_text(
                    i18n.get('staff.bottles.session_not_found', language),
                    reply_markup=CommonKeyboards.back_button(language, 'bottles_join_session'),
                )
                return

            owner_name = session_info.get('owner_name') or 'Driver'
            inventory = session_info.get('current_inventory', 0)
            loaded = session_info.get('bottles_loaded', 0)

            text = (
                f"🤝 <b>{i18n.get('staff.bottles.join_session_confirm_title', language)}</b>\n\n"
                f"👤 {i18n.get('staff.bottles.session_owner', language)}: <b>{owner_name}</b>\n"
                f"📦 {i18n.get('staff.bottles.bottles_on_truck', language)}: <b>{inventory}</b> / {loaded}\n\n"
                f"{i18n.get('staff.bottles.join_session_confirm_note', language)}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ {i18n.get('staff.bottles.confirm_join', language)}",
                    callback_data=f"bottles_join_execute_{session_id}",
                )],
                [InlineKeyboardButton(
                    i18n.get('common.cancel', language),
                    callback_data='bottles_join_session',
                )],
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing join confirmation: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def execute_join_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute the join session request."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Callback: bottles_join_execute_{session_id}
            session_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.join_bottle_session(token, session_id)

            if not response.success:
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                await query.edit_message_text(
                    f"❌ {error_msg}",
                    reply_markup=CommonKeyboards.back_button(language, 'bottles_join_session'),
                    parse_mode='HTML',
                )
                return

            membership = response.data or {}
            owner_name = membership.get('owner_name') or 'Driver'

            text = (
                f"✅ <b>"
                f"{i18n.get('staff.bottles.joined_session', language).format(name=owner_name)}"
                f"</b>\n\n"
                f"{i18n.get('staff.bottles.joined_session_info', language)}"
            )
            await query.edit_message_text(
                text,
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                parse_mode='HTML',
            )

        except Exception as e:
            logger.error(f"Error executing join session: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def leave_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Leave the current co-driver session membership."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.leave_bottle_session(token)

            if not response.success:
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                await query.edit_message_text(
                    f"❌ {error_msg}",
                    reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                    parse_mode='HTML',
                )
                return

            await query.edit_message_text(
                f"✅ {i18n.get('staff.bottles.left_session', language)}",
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
            )

        except Exception as e:
            logger.error(f"Error leaving session: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def show_membership_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the driver's current co-driver membership status."""
        query = update.callback_query
        if query:
            await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_current_session_membership(token)

            if not response.success:
                if response.status_code == 404:
                    text = i18n.get('staff.bottles.no_active_membership', language)
                else:
                    text = self._resolve_api_error_message(
                        language,
                        error=getattr(response, 'error', None),
                        status_code=getattr(response, 'status_code', None),
                        error_code=getattr(response, 'error_code', None),
                    )
                keyboard = CommonKeyboards.back_button(language, 'staff_back_to_main')
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard)
                else:
                    await update.message.reply_text(text, reply_markup=keyboard)
                return

            m = response.data or {}
            owner_name = m.get('owner_name') or 'Driver'
            inventory = m.get('current_inventory', 0)

            text = (
                f"🤝 <b>{i18n.get('staff.bottles.current_membership_title', language)}</b>\n\n"
                + i18n.get('staff.bottles.current_membership', language).format(
                    name=owner_name, qty=inventory
                )
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🚪 {i18n.get('staff.bottles.leave_session', language)}",
                    callback_data='bottles_leave_session',
                )],
                [InlineKeyboardButton(
                    i18n.get('common.back', language),
                    callback_data='staff_back_to_main',
                )],
            ])
            if query:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing membership status: {e}", exc_info=True)
            await self._handle_error(update, context)

    # ------------------------------------------------------------------
    # Session owner: invite a driver to join their session
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def show_invitable_drivers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show drivers the session owner can invite to their open session."""
        query = update.callback_query
        if query:
            await query.answer()

        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_drivers_available_to_invite(token)

            if not response.success:
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                text = f"❌ {error_msg}"
                keyboard = CommonKeyboards.back_button(language, 'staff_back_to_main')
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            drivers = response.data or []
            if not drivers:
                text = i18n.get('staff.bottles.no_drivers_to_invite', language)
                keyboard = CommonKeyboards.back_button(language, 'staff_back_to_main')
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard)
                else:
                    await update.message.reply_text(text, reply_markup=keyboard)
                return

            buttons = []
            for d in drivers:
                name = d.get('name') or f"Driver #{d['user_id']}"
                buttons.append([
                    InlineKeyboardButton(
                        f"👤 {name}",
                        callback_data=f"bottles_invite_confirm_{d['user_id']}",
                    )
                ])
            buttons.append([
                InlineKeyboardButton(i18n.get('common.back', language), callback_data='staff_back_to_main')
            ])

            text = f"<b>{i18n.get('staff.bottles.invite_codriver', language)}</b>"
            keyboard = InlineKeyboardMarkup(buttons)
            if query:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing invitable drivers: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_invite_driver(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show confirmation before inviting a driver."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            driver_id = int(query.data.split('_')[-1])
            context.user_data['pending_invite_driver_id'] = driver_id

            # Fetch drivers list again to get the name
            async with api_client as client:
                response = await client.get_drivers_available_to_invite(token)

            driver_info = None
            if response.success and response.data:
                driver_info = next((d for d in response.data if d['user_id'] == driver_id), None)

            name = driver_info.get('name') if driver_info else f"Driver #{driver_id}"

            text = (
                f"🤝 <b>{i18n.get('staff.bottles.invite_codriver_confirm', language)}</b>\n\n"
                f"👤 <b>{name}</b>\n\n"
                f"{i18n.get('staff.bottles.invite_codriver_confirm_note', language)}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ {i18n.get('staff.bottles.confirm_invite', language)}",
                    callback_data=f"bottles_invite_execute_{driver_id}",
                )],
                [InlineKeyboardButton(
                    i18n.get('common.cancel', language),
                    callback_data='bottles_invite_driver',
                )],
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing invite confirmation: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def execute_invite_driver(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute the invite — add driver to current session."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            driver_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.invite_driver_to_session(token, driver_id)

            if not response.success:
                error_msg = self._resolve_api_error_message(
                    language,
                    error=getattr(response, 'error', None),
                    status_code=getattr(response, 'status_code', None),
                    error_code=getattr(response, 'error_code', None),
                )
                await query.edit_message_text(
                    f"❌ {error_msg}",
                    reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                    parse_mode='HTML',
                )
                return

            membership = response.data or {}
            member_name = membership.get('member_name') or f"Driver #{driver_id}"

            text = (
                f"✅ <b>"
                f"{i18n.get('staff.bottles.codriver_invited', language).format(name=member_name)}"
                f"</b>"
            )
            await query.edit_message_text(
                text,
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                parse_mode='HTML',
            )

        except Exception as e:
            logger.error(f"Error executing driver invite: {e}", exc_info=True)
            await self._handle_error(update, context)
