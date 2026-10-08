"""Co-driver session join/leave flow for delivery drivers.

Allows a driver to join another driver's open bottle session (e.g. two drivers
sharing the same truck). The handler provides:
  - List of joinable sessions
  - Join request (owner approves) and the owner's answer
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
from staff_bot.utils.formatters import escape_html

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
                error_msg = self._resolve_response_error(language, response, html=True)
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
                owner_name = s.get('owner_name') or i18n.get('staff.common.unknown_driver', language)
                inventory = s.get('current_inventory', 0)
                loaded = s.get('bottles_loaded', 0)
                label = f"📦 {owner_name} — {inventory}/{loaded}"
                buttons.append([
                    InlineKeyboardButton(label, callback_data=f"bottles_join_confirm_{s['session_id']}")
                ])
            buttons.append([
                InlineKeyboardButton(
                    i18n.get('staff.back', language),
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

            if not response.success:
                # A failed read is not "session not found": say why.
                await query.edit_message_text(
                    f"❌ {self._resolve_response_error(language, response)}",
                    reply_markup=CommonKeyboards.back_button(language, 'bottles_join_session'),
                )
                return

            session_info = None
            if response.data:
                session_info = next(
                    (s for s in response.data if s['session_id'] == session_id), None
                )

            if not session_info:
                await query.edit_message_text(
                    i18n.get('staff.bottles.session_not_found', language),
                    reply_markup=CommonKeyboards.back_button(language, 'bottles_join_session'),
                )
                return

            owner_name = session_info.get('owner_name') or i18n.get('staff.common.unknown_driver', language)
            inventory = session_info.get('current_inventory', 0)
            loaded = session_info.get('bottles_loaded', 0)

            text = (
                f"🤝 <b>{i18n.get('staff.bottles.join_session_confirm_title', language)}</b>\n\n"
                f"👤 {i18n.get('staff.bottles.session_owner', language)}: <b>{escape_html(owner_name)}</b>\n"
                f"📦 {i18n.get('staff.bottles.bottles_on_truck', language)}: <b>{inventory}</b> / {loaded}\n\n"
                f"{i18n.get('staff.bottles.join_session_confirm_note', language)}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ {i18n.get('staff.bottles.confirm_join', language)}",
                    callback_data=f"bottles_join_request_{session_id}",
                )],
                [InlineKeyboardButton(
                    i18n.get('staff.cancel', language),
                    callback_data='bottles_join_session',
                )],
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing join confirmation: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def send_join_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ask the session owner to let this driver join; the owner answers in their own chat."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Callback: bottles_join_request_{session_id}
            session_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.request_to_join_bottle_session(token, session_id)

            if not response.success:
                await query.edit_message_text(
                    f"❌ {self._resolve_response_error(language, response, html=True)}",
                    reply_markup=CommonKeyboards.back_button(language, 'bottles_join_session'),
                    parse_mode='HTML',
                )
                return

            owner_name = (response.data or {}).get('owner_name') or i18n.get('staff.common.unknown_driver', language)
            await query.edit_message_text(
                i18n.get('staff.bottles.join_request_sent', language, name=escape_html(owner_name)),
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                parse_mode='HTML',
            )
            context.user_data.pop('pending_join_session_id', None)

        except Exception as e:
            logger.error(f"Error sending join request: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def answer_join_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The session owner taps Approve / Decline on a colleague's pushed join request."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Callback: bottles_jr_{ok|no}_{session_id}_{requester_id}
            _prefix, _jr, verdict, session_id, requester_id = query.data.split('_')
            session_id, requester_id = int(session_id), int(requester_id)

            async with api_client as client:
                if verdict == 'ok':
                    response = await client.approve_join_request(token, session_id, requester_id)
                else:
                    response = await client.decline_join_request(token, session_id, requester_id)

            if not response.success:
                # A transient failure (no code, or a 5xx) leaves the request open:
                # keep Approve / Decline so the owner can tap again.
                status_code = getattr(response, 'status_code', None)
                transient = not getattr(response, 'error_code', None) or (status_code or 0) >= 500
                await query.edit_message_text(
                    f"❌ {self._resolve_response_error(language, response, html=True)}",
                    reply_markup=(
                        query.message.reply_markup if transient
                        else CommonKeyboards.back_button(language, 'staff_back_to_main')
                    ),
                    parse_mode='HTML',
                )
                return

            data = response.data or {}
            unknown = i18n.get('staff.common.unknown_driver', language)
            if verdict == 'ok':
                text = i18n.get(
                    'staff.bottles.join_request_approved_owner', language,
                    name=escape_html(data.get('member_name') or unknown),
                )
            else:
                text = i18n.get(
                    'staff.bottles.join_request_declined_owner', language,
                    name=escape_html(data.get('requester_name') or unknown),
                )
            # Edited in place: the Approve / Decline buttons go, so they cannot be tapped again.
            await query.edit_message_text(
                text,
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                parse_mode='HTML',
            )

        except Exception as e:
            logger.error(f"Error answering join request: {e}", exc_info=True)
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
                error_msg = self._resolve_response_error(language, response, html=True)
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
                # Only "not a member" is the empty state. A closed session
                # (BOTTLE_SESSION_MEMBERSHIP_CLOSED, also a 404) and every other
                # refusal go through the resolver.
                if getattr(response, 'error_code', None) == 'BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND':
                    text = i18n.get('staff.bottles.no_active_membership', language)
                else:
                    text = self._resolve_response_error(language, response)
                keyboard = CommonKeyboards.back_button(language, 'staff_back_to_main')
                if query:
                    await query.edit_message_text(text, reply_markup=keyboard)
                else:
                    await update.message.reply_text(text, reply_markup=keyboard)
                return

            m = response.data or {}
            owner_name = m.get('owner_name') or i18n.get('staff.common.unknown_driver', language)
            inventory = m.get('current_inventory', 0)

            membership_line = i18n.get(
                'staff.bottles.current_membership', language,
                name=escape_html(owner_name), qty=inventory,
            )
            text = (
                f"🤝 <b>{i18n.get('staff.bottles.current_membership_title', language)}</b>\n\n"
                + membership_line
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🚪 {i18n.get('staff.bottles.leave_session', language)}",
                    callback_data='bottles_leave_session',
                )],
                [InlineKeyboardButton(
                    i18n.get('staff.back', language),
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
                error_msg = self._resolve_response_error(language, response, html=True)
                text = f"❌ {error_msg}"
                keyboard = self._with_remedy(
                    language, response.error_code,
                    CommonKeyboards.back_button(language, 'staff_back_to_main'),
                )
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
                name = d.get('name') or i18n.get(
                    'staff.common.driver_number', language, driver_id=d['user_id']
                )
                buttons.append([
                    InlineKeyboardButton(
                        f"👤 {name}",
                        callback_data=f"bottles_invite_confirm_{d['user_id']}",
                    )
                ])
            buttons.append([
                InlineKeyboardButton(i18n.get('staff.back', language), callback_data='staff_back_to_main')
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

            if not response.success:
                # A failed read is not "invite this driver anyway": say why.
                await query.edit_message_text(
                    f"❌ {self._resolve_response_error(language, response)}",
                    reply_markup=self._with_remedy(
                        language, response.error_code,
                        CommonKeyboards.back_button(language, 'bottles_invite_driver'),
                    ),
                )
                return

            driver_info = None
            if response.data:
                driver_info = next((d for d in response.data if d['user_id'] == driver_id), None)

            name = driver_info.get('name') if driver_info else i18n.get(
                'staff.common.driver_number', language, driver_id=driver_id
            )

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
                    i18n.get('staff.cancel', language),
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
                error_msg = self._resolve_response_error(language, response, html=True)
                await query.edit_message_text(
                    f"❌ {error_msg}",
                    reply_markup=self._with_remedy(
                        language, response.error_code,
                        CommonKeyboards.back_button(language, 'staff_back_to_main'),
                    ),
                    parse_mode='HTML',
                )
                return

            membership = response.data or {}
            member_name = membership.get('member_name') or i18n.get(
                'staff.common.driver_number', language, driver_id=driver_id
            )

            # NOT wrapped in `✅ <b>…</b>` the way the join-approved push
            # decorates its sentence: this row is seeded as
            # "✅ <b>{name}</b> has been added…" in all three languages, so a
            # wrapper here produced a doubled tick and NESTED <b> tags under
            # parse_mode='HTML'. The copy already carries its own decoration;
            # render it as it ships.
            text = i18n.get(
                'staff.bottles.codriver_invited', language, name=escape_html(member_name)
            )
            await query.edit_message_text(
                text,
                reply_markup=CommonKeyboards.back_button(language, 'staff_back_to_main'),
                parse_mode='HTML',
            )

        except Exception as e:
            logger.error(f"Error executing driver invite: {e}", exc_info=True)
            await self._handle_error(update, context)
