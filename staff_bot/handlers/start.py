"""
/start handler for Staff Bot - Staff authentication flow.
Staff authenticate by pre-bound Telegram ID or one-time invite token.
"""
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.api_client import api_client
from staff_bot.keyboards.menu import MenuKeyboards
from shared.staff_constants import STAFF_BOT_ROLES

logger = logging.getLogger(__name__)

# Conversation states
SELECT_LANGUAGE = 0


class StartHandler(BaseHandler):
    """Handles /start command and staff authentication"""

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point: /start command"""
        user_id = update.effective_user.id
        logger.info(f"Staff bot /start from user {user_id}")
        context.user_data.pop('invite_token', None)
        # /start is a hard reset to the top of the bot. Drop any in-progress flow
        # flags so a driver who restarts mid-collection isn't left with a stale
        # flow that mis-routes their next text update.
        from staff_bot.utils import flow_state
        await flow_state.clear_pending_flows(context, update)

        # Check if user is already authenticated
        if context.user_data.get('authenticated'):
            user = await self.user_repo.get_user_by_telegram_id(user_id)
            if user and user.get('staff_roles'):
                language = i18n.normalize_language(context.user_data.get('language'))
                context.user_data['language'] = language
                await update.message.reply_text(
                    i18n.get('staff.welcome_back', language, name=user.get('first_name', '')),
                    reply_markup=MenuKeyboards.main_menu(
                        language, context.user_data.get('staff_roles', [])
                    )
                )
                return ConversationHandler.END

        # Check if telegram_id is already linked to a staff account
        user = await self.user_repo.get_user_by_telegram_id(user_id)
        if user:
            staff_roles = user.get('staff_roles') or []
            if isinstance(staff_roles, str):
                import json
                staff_roles = json.loads(staff_roles)

            if any(role in STAFF_BOT_ROLES for role in staff_roles):
                # Already linked - authenticate via backend to issue fresh JWT.
                context.user_data['language'] = i18n.normalize_language(
                    user.get('preferred_language') or context.user_data.get('language')
                )
                return await self._authenticate_with_binding(update, context)

        # Capture optional invite token passed via deep link
        invite_arg = context.args[0].strip() if getattr(context, 'args', None) else ''
        if invite_arg.startswith('staff_invite_'):
            context.user_data['invite_token'] = invite_arg[len('staff_invite_'):]
        elif invite_arg:
            # Backward compatibility for short links that may pass raw token
            context.user_data['invite_token'] = invite_arg

        # Not linked yet - show language selection then auth attempt
        keyboard = []
        for lang_code in ['en', 'uz', 'ru']:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, lang_code)
            keyboard.append([f"{flag} {name}"])

        # The staff member has not picked a language yet, so lean on Telegram's
        # own client locale before falling back to the deployment default.
        # Hardcoding 'en' here made the very first screen a new driver sees
        # English for everyone, in a fleet whose DEFAULT_LANGUAGE is 'uz'.
        await update.message.reply_text(
            i18n.get('staff.welcome_intro', self._preferred_language(update, context)),
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SELECT_LANGUAGE

    @staticmethod
    def _preferred_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Best guess at a language for a staff member who has not chosen one.

        Order: a language already captured this session -> the Telegram client
        locale -> `config.localization.default_language`. `normalize_language`
        maps locale variants ('ru-RU', 'uz-Latn') onto supported codes and
        returns the configured default for anything it cannot place.
        """
        captured = (context.user_data or {}).get('language')
        if captured:
            return i18n.normalize_language(captured)

        telegram_locale = getattr(update.effective_user, 'language_code', None)
        return i18n.normalize_language(telegram_locale)

    async def language_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection then authenticate by Telegram binding."""
        text = update.message.text.strip()

        # Parse language from button text
        language = 'en'
        if 'O\'zbekcha' in text or '🇺🇿' in text:
            language = 'uz'
        elif 'Русский' in text or '🇷🇺' in text:
            language = 'ru'
        elif 'English' in text or '🇺🇸' in text:
            language = 'en'

        context.user_data['language'] = i18n.normalize_language(language)
        logger.info(f"Staff user {update.effective_user.id} selected language: {language}")

        return await self._authenticate_with_binding(update, context)

    async def _authenticate_with_binding(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Authenticate via pre-bound Telegram ID or one-time invite token."""
        language = i18n.normalize_language(context.user_data.get('language'))
        context.user_data['language'] = language
        user_id = update.effective_user.id
        invite_token = context.user_data.get('invite_token')

        logger.info(
            "Staff authentication attempt: telegram_id=%s invite_token_present=%s",
            user_id,
            bool(invite_token),
        )

        async with api_client as client:
            response = await client.staff_login(user_id, invite_token)

        if response.success:
            data = response.data or {}
            user_data = data.get('user', {})
            staff_roles = user_data.get('staff_roles', [])
            access_token = data.get('access_token')
            refresh_token = data.get('refresh_token')

            # Store tokens
            if access_token:
                context.user_data['access_token'] = access_token
                token_manager = context.bot_data.get('token_manager')
                if token_manager and refresh_token:
                    await token_manager.store_tokens(
                        user_id, access_token, refresh_token,
                        data.get('expires_in', 3600)
                    )

            # Update language in DB
            await self.user_repo.update_user_language(user_id, language)

            return await self._complete_login(update, context, user_data, staff_roles)
        else:
            error = self._resolve_api_error_message(
                language,
                response.error,
                status_code=response.status_code,
                error_code=getattr(response, 'error_code', None),
            )
            if response.status_code in (403, 404):
                if getattr(response, 'error_code', None) == 'STAFF_ACCOUNT_DEACTIVATED':
                    login_message = i18n.get('staff.account_deactivated', language)
                else:
                    login_message = i18n.get('staff.not_staff', language)
                await update.message.reply_text(
                    login_message,
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                await update.message.reply_text(
                    i18n.get('staff.login_failed', language, error=error),
                    reply_markup=ReplyKeyboardRemove()
                )
            return ConversationHandler.END

    async def _complete_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                               user_data: dict, staff_roles: list):
        """Complete login and show main menu"""
        language = i18n.normalize_language(context.user_data.get('language'))
        context.user_data['language'] = language
        context.user_data.pop('invite_token', None)

        context.user_data['authenticated'] = True
        context.user_data['user_id'] = user_data.get('id')
        context.user_data['staff_roles'] = staff_roles
        context.user_data['first_name'] = user_data.get('first_name', '')
        context.user_data['last_name'] = user_data.get('last_name', '')
        context.user_data['phone'] = user_data.get('phone', '')
        context.user_data['delivery_person_id'] = user_data.get('delivery_person_id')

        # Log activity
        if user_data.get('id'):
            await self.user_repo.log_staff_activity(
                user_data['id'], 'staff_login',
                metadata={'roles': staff_roles}
            )

        # Format role names for display
        role_display = ', '.join(
            i18n.get(f'staff.role.{role}', language) for role in staff_roles
        )

        name = user_data.get('first_name', '')

        if update.message:
            await update.message.reply_text(
                i18n.get('staff.login_success', language, name=name, role=role_display),
                reply_markup=MenuKeyboards.main_menu(language, staff_roles)
            )
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                i18n.get('staff.login_success', language, name=name, role=role_display)
            )
            await update.callback_query.message.reply_text(
                i18n.get('staff.menu.title', language),
                reply_markup=MenuKeyboards.main_menu(language, staff_roles)
            )

        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel authentication flow"""
        language = self._preferred_language(update, context)
        context.user_data.pop('invite_token', None)
        await update.message.reply_text(
            i18n.get('staff.auth_cancelled', language),
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
