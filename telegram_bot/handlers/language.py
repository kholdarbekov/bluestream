"""
Language selection handlers
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from eligibility import main_menu_for
from i18n import i18n
from keyboards import LanguageKeyboards, MenuKeyboards
from database import db_manager, BotUserRepository
from utils import user_middleware, get_auth_token
from api_client import api_client
from config import config

logger = logging.getLogger(__name__)


class LanguageHandler:
    """Language selection handler class"""

    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)

    async def language_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show language selection menu"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            current_language = await i18n.get_user_language(user_id)

            # Build enhanced language selection message
            current_flag = i18n.get_language_flag(current_language)
            current_name = i18n.get_language_name(current_language, current_language)

            menu_text = f"🌐 {i18n.get('telegram.menu.language', current_language)}\n\n"
            menu_text += f"{i18n.get('telegram.language.current', current_language)}: {current_flag} {current_name}\n\n"
            menu_text += f"{i18n.get('telegram.language.select_prompt', current_language)}"

            keyboard = LanguageKeyboards.language_selection(current_language)

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=menu_text,
                    reply_markup=keyboard
                )

            logger.info(f"Language menu displayed for user {user_id} (current: {current_language})")

        except Exception as e:
            logger.error(f"Error in language menu: {e}")

            try:
                language = await i18n.get_user_language(update.effective_user.id)
                error_msg = i18n.get('telegram.error_occurred', language)

                if update.callback_query:
                    await update.callback_query.answer(error_msg)
                else:
                    await update.message.reply_text(error_msg)
            except Exception as e:
                logger.warning(f"Failed to send error message in language handler fallback: {e}")

    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id

            # Extract language code from callback data
            # Index 2 for the same reason as profile.language_selection: the
            # signup keyboard may append a `_ref<code>` suffix.
            language_code = query.data.split('_')[2]

            # A LANGUAGE TAP FROM SOMEONE WITH NO ACCOUNT IS A SIGNUP, NOT A
            # CHANGE. `^set_language_` is registered twice: here in group 0 for
            # Profile -> Language, and inside the registration conversation's
            # SELECT_LANGUAGE state, where `language_selection` is what actually
            # calls `register_telegram_user`. The conversation state map dies
            # with the process, so after any deploy a brand-new customer's very
            # first tap fell through to THIS handler — which went on to
            # `update_user_language`, an UPDATE matching zero rows, and then
            # showed a full main menu for an account that does not exist.
            # Signup dead-ended at step one with nothing said.
            #
            # Delegated rather than reimplemented: registration also caches the
            # fresh tokens and consumes the referral, and a second copy of that
            # is how the two would drift. Imported locally to keep this module
            # free of a package-level cycle with handlers.profile.
            # tests/telegram_bot/test_signup_journey_after_restart.py
            # Fail SAFE, and deliberately asymmetric: only a lookup that
            # positively says "no such account" delegates. If the lookup itself
            # cannot answer — a transient DB error, a repo that does not
            # implement it — we must assume the account exists, because the cost
            # of guessing wrong in that direction is re-running registration for
            # somebody who already has a row.
            try:
                existing_user = await self.user_repo.get_user_by_telegram_id(user_id)
            except Exception as lookup_error:
                logger.warning(
                    "Could not establish whether %s has an account (%s); treating "
                    "the tap as a language change, not a signup.",
                    user_id, lookup_error,
                )
                existing_user = True
            if not existing_user:
                from handlers.profile import profile_handlers
                await profile_handlers.language_selection(update, context)
                return

            # Get current language before change
            current_language = await i18n.get_user_language(user_id)

            # Validate language code
            if language_code not in config.localization.supported_languages:
                await query.answer(i18n.get('telegram.language.invalid_selection', current_language))
                return

            # Check if already using this language
            if language_code == current_language:
                flag = i18n.get_language_flag(language_code)
                language_name = i18n.get_language_name(language_code, language_code)
                already_using_msg = i18n.get('telegram.language.already_selected', language_code)
                await query.answer(f"{flag} {already_using_msg}")
                return

            # Update user language in database
            await self.user_repo.update_user_language(user_id, language_code)

            # Keep the backend's preferred_language in sync (best-effort: a backend
            # failure must NOT break the bot-only language change).
            try:
                async with api_client as client:
                    user_token = await get_auth_token(update, context, client)
                    if user_token:
                        await client.update_user_profile(
                            user_token, {'preferred_language': language_code}
                        )
            except Exception as sync_exc:
                logger.warning(f"Failed to sync language to backend for {user_id}: {sync_exc}")

            # Build comprehensive success message with language preview
            flag = i18n.get_language_flag(language_code)
            language_name = i18n.get_language_name(language_code, language_code)

            # Show popup notification
            success_msg = i18n.get('telegram.language.changed_success', language_code)
            await query.answer(f"{flag} {success_msg}", show_alert=False)

            # Build detailed confirmation message showing language change
            confirmation_text = f"{flag} {i18n.get('telegram.language.confirmation_title', language_code)}\n\n"
            # The value goes INTO `get()`. `i18n.get()` no longer returns a
            # fillable template: copy the caller does not fill is treated as
            # broken and degraded to the humanised key, so formatting its
            # RESULT rendered "✅ Now using" — the confirmation for a language
            # switch, with the language missing.
            #
            # `language_name=` matches the seeded placeholder. It was renamed
            # from `{language}` on 2026-08-22, back when `Translation.get`
            # accepted `key`/`language` BY KEYWORD and therefore owned those
            # names: passing `language=` raised "got multiple values for
            # argument 'language'", so a `{language}` row was unfillable through
            # the only interface allowed to fill it. Both parameters are
            # positional-only now, so that collision cannot recur and the name
            # here is simply the one the row uses — a DB row still carrying the
            # old `{language}` degrades to the humanised key until the seed is
            # re-run.
            now_using_text = i18n.get(
                'telegram.language.now_using',
                language_code,
                language_name=language_name,
            )
            confirmation_text += f"✅ {now_using_text}\n\n"
            confirmation_text += f"{i18n.get('telegram.language.confirmation_message', language_code)}"

            # Return to main menu with new language
            keyboard = await main_menu_for(update.effective_user.id, language_code)

            await query.edit_message_text(
                text=confirmation_text,
                reply_markup=keyboard
            )

            logger.info(f"User {user_id} changed language from {current_language} to {language_code}")

        except Exception as e:
            logger.error(f"Error setting language: {e}")

            try:
                current_language = await i18n.get_user_language(user_id)
                await update.callback_query.answer(i18n.get('telegram.language.error_changing', current_language))
            except Exception as e:
                logger.warning(f"Failed to send error message in set_language fallback: {e}")


# Create global handler instance
language_handler = LanguageHandler()
