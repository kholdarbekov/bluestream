"""
User profile and registration handlers
"""
import logging
from typing import Dict, Any
from telegram import constants, Update, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from i18n import i18n
from keyboards import ProfileKeyboards, MenuKeyboards, LanguageKeyboards, KeyboardBuilder
from shared.constants import TASHKENT_DISTRICTS, get_district_name, get_district_center, get_all_districts
from handlers.menu import main_menu_handler
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, validate_phone_number, normalize_phone_number, get_auth_token
from config import config

logger = logging.getLogger('handlers')

# Conversation states
(SELECT_LANGUAGE, PHONE, NAME, ADDRESS_LOCATION, ADDRESS_TITLE,
 ADDRESS_REGION, ADDRESS_DISTRICT, ADDRESS_STREET, ADDRESS_BUILDING,
 ADDRESS_APARTMENT, ADDRESS_FLOOR, ADDRESS_ENTRANCE,
 ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
 PHONE_VERIFY_PHONE, PHONE_VERIFY_NAME,
 LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP) = range(18)


class ProfileHandlers:
    """Profile management handlers"""
    
    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)
    
    async def profile_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user profile menu"""
        try:
            user = await user_middleware(update)
            if not user:
                return
            
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Clear any pending input state
            await self.user_repo.update_user_state(user_id, {})
            
            # Get user profile from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_profile(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                profile = response.data['data']
            
            full_name = (f"{profile.get('first_name', '')} {profile.get('last_name', '')}" or "Not set").strip()
            
            # Format profile information
            profile_text = f"{i18n.get('telegram.profile_title', language)}\n\n"
            profile_text += f"{i18n.get('telegram.profile_name', language)}: {full_name}\n"
            profile_text += f"{i18n.get('telegram.profile_phone', language)}: {profile.get('phone', 'Not set')}\n"
            profile_text += f"{i18n.get('telegram.profile_email', language)}: {profile.get('email', 'Not set')}\n"
            profile_text += f"{i18n.get('telegram.profile_language', language)}: {language}"
            
            keyboard = ProfileKeyboards.profile_menu(language)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=profile_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=profile_text,
                    reply_markup=keyboard
                )
            
            logger.info(f"Profile menu shown to user {user_id}")

        except Exception as e:
            logger.error(f"Error in profile menu: {e}")
            await self._handle_error(update)

    async def phone_verification_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show phone verification menu with add/verify options"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user profile from API to check phone status
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_profile(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                profile = response.data['data']

            phone = profile.get('phone')
            phone_verified = profile.get('phone_verified_at') is not None or profile.get('phone_verified', False)

            # Build status message
            if not phone:
                status_text = i18n.get('telegram.phone.title', language) + "\n\n" + i18n.get('telegram.phone.no_phone_added', language)
                buttons = [
                    [{'text': i18n.get('telegram.phone.add_prompt', language), 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]
            elif not phone_verified:
                status_text = i18n.get('telegram.phone.title', language) + f"\n\n{i18n.get('telegram.profile_phone', language)}: {phone}\n" + i18n.get('telegram.phone.phone_not_verified', language)
                buttons = [
                    [{'text': i18n.get('telegram.phone.verification_prompt', language), 'callback_data': 'verify_phone_number'}],
                    [{'text': '📝 Change Phone Number', 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]
            else:
                status_text = i18n.get('telegram.phone.title', language) + f"\n\n{i18n.get('telegram.profile_phone', language)}: {phone}\n" + i18n.get('telegram.phone.phone_verified', language)
                buttons = [
                    [{'text': '📝 Change Phone Number', 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=status_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=status_text,
                    reply_markup=keyboard
                )

            logger.info(f"Phone verification menu shown to user {user_id}")

        except Exception as e:
            logger.error(f"Error in phone verification menu: {e}")
            await self._handle_error(update)

    async def add_phone_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone number addition/change flow - entry point for phone verification conversation"""
        try:
            user = await user_middleware(update)
            if not user:
                return ConversationHandler.END

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Prompt user to share phone
            phone_prompt = i18n.get('telegram.phone.send_code_prompt', language)

            # Send the prompt with reply keyboard
            keyboard = ProfileKeyboards.phone_request(language)

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(
                    phone_prompt,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    phone_prompt,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )

            logger.info(f"Phone addition flow started for user {user_id}, entering PHONE_VERIFY_PHONE state")
            return PHONE_VERIFY_PHONE

        except Exception as e:
            logger.error(f"Error starting phone addition: {e}")
            await self._handle_error(update)
            return ConversationHandler.END

    async def phone_verify_contact_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone contact shared during phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            contact = update.message.contact

            # Verify the contact belongs to the user
            if contact.user_id != user_id:
                await update.message.reply_text(
                    "❌ Please share your own phone number.",
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE_VERIFY_PHONE

            phone = normalize_phone_number(contact.phone_number)
            logger.info(f"Phone contact received for user {user_id}: {phone}")

            # Store phone in context for later
            context.user_data['pending_phone'] = phone

            # Update phone in database immediately
            await self.user_repo.set_user_phone(user_id, phone)

            # Also update via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    try:
                        await client.update_user_profile(user_token, {'phone': phone})
                        logger.info(f"Phone updated via API for user {user_id}")
                    except Exception as api_error:
                        logger.warning(f"Failed to update phone via API: {api_error}")

            # Remove the phone request keyboard and ask for name
            success_text = i18n.get('telegram.phone.phone_accepted', language) or "✅ Telefon raqami qabul qilindi!"
            await update.message.reply_text(
                success_text,
                reply_markup=ReplyKeyboardRemove()
            )

            # Ask for full name
            name_prompt = i18n.get('telegram.enter_name', language) or "👤 Iltimos to'liq ismingizni kiriting:"
            await update.message.reply_text(name_prompt)

            logger.info(f"Phone accepted for user {user_id}, asking for name")
            return PHONE_VERIFY_NAME

        except Exception as e:
            logger.error(f"Error in phone_verify_contact_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def phone_verify_name_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle name input during phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            text = update.message.text.strip()

            logger.info(f"Name received for user {user_id}: {text}")

            # Validate name - must have at least 2 characters and contain letters
            if len(text) < 2:
                await update.message.reply_text(
                    i18n.get('telegram.name.too_short', language) or "❌ Ism juda qisqa. Kamida 2 ta belgi kiriting."
                )
                return PHONE_VERIFY_NAME

            # Check for valid name (letters and spaces only)
            if not any(c.isalpha() for c in text):
                await update.message.reply_text(
                    i18n.get('telegram.name.invalid', language) or "❌ Noto'g'ri ma'lumot. Qaytadan urinib ko'ring."
                )
                return PHONE_VERIFY_NAME

            # Parse first and last name
            name_parts = text.split()
            first_name = name_parts[0]
            last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''

            # Update profile via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    profile_data = {
                        'first_name': first_name,
                        'last_name': last_name
                    }
                    response = await client.update_user_profile(user_token, profile_data)
                    if response.success:
                        logger.info(f"Name updated via API for user {user_id}: {first_name} {last_name}")
                    else:
                        logger.warning(f"Failed to update name via API: {response.error}")

            # Show success and main menu
            success_text = i18n.get('telegram.profile_updated', language) or "✅ Profil muvaffaqiyatli yangilandi!"
            keyboard = MenuKeyboards.main_menu(language)

            await update.message.reply_text(
                text=success_text,
                reply_markup=keyboard
            )

            # Clear pending phone from context
            context.user_data.pop('pending_phone', None)

            logger.info(f"Phone verification flow completed for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in phone_verify_name_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def cancel_phone_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)
            keyboard = MenuKeyboards.main_menu(language)

            # Clear pending data
            context.user_data.pop('pending_phone', None)

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )

            logger.info(f"Phone verification cancelled for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error cancelling phone verification: {e}")
            return ConversationHandler.END

    async def verify_phone_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone verification flow for existing phone"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user's phone from profile
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_profile(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                profile = response.data['data']
                phone = profile.get('phone')

            if not phone:
                await update.callback_query.answer(i18n.get('telegram.phone.no_phone_added', language))
                await update.callback_query.message.reply_text(
                    i18n.get('telegram.phone.no_phone_added', language)
                )
                return

            # Send verification code
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.send_phone_verification(user_token, phone)
                    if response.success:
                        verification_msg = (
                            "📱 *Phone Verification*\n\n"
                            f"An SMS with a verification code has been sent to *{phone}*.\n\n"
                            "Please reply with the 6-digit code to verify your phone number."
                        )

                        await update.callback_query.answer("Verification code sent!")
                        await update.callback_query.message.reply_text(
                            verification_msg,
                            parse_mode='Markdown'
                        )

                        # Store awaiting OTP flag
                        context.user_data['awaiting_otp'] = True
                        context.user_data['pending_phone_verification'] = phone

                        logger.info(f"Verification SMS sent to {phone} for user {user_id}")
                    else:
                        await update.callback_query.answer("Failed to send code!")
                        await update.callback_query.message.reply_text(
                            f"❌ Could not send verification SMS: {response.error}\n\n"
                            "Please try again later."
                        )

        except Exception as e:
            logger.error(f"Error in phone verification: {e}")
            await self._handle_error(update)

    async def start_registration_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start registration process"""
        try:
            user_id = update.effective_user.id
            telegram_language_code = update.effective_user.language_code

            user_repo = BotUserRepository(db_manager)
            # Check if user already exists
            existing_user = await user_repo.get_user_by_telegram_id(user_id)
            logger.info(f"existing_user {existing_user} for user_id {user_id}")

            if not existing_user:
                welcome_text_en = i18n.get('telegram.registration_welcome', "en")
                welcome_text_uz = i18n.get('telegram.registration_welcome', "uz")
                welcome_text_ru = i18n.get('telegram.registration_welcome', "ru")
                welcome_text = f"{welcome_text_en}\n\n{welcome_text_uz}\n\n{welcome_text_ru}"

                await update.message.reply_text(
                    welcome_text,
                    reply_markup=LanguageKeyboards.select_language()
                )

                return SELECT_LANGUAGE
            else:
                # Already registered, show main menu
                complete_text = i18n.get('telegram.welcome', telegram_language_code)
                keyboard = MenuKeyboards.main_menu(telegram_language_code)

                await update.message.reply_text(
                    text=complete_text,
                    reply_markup=keyboard
                )

                return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Error starting registration: {e}")
            return ConversationHandler.END
    
    async def start_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start registration process"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Ask for phone number
            phone_text = i18n.get('telegram.registration.enter_phone', language)
            keyboard = ProfileKeyboards.phone_request(language)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=phone_text
                )
                await update.callback_query.answer()
                # Send new message with keyboard
                await update.callback_query.message.reply_text(
                    text="Please share your contact:",
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=phone_text,
                    reply_markup=keyboard
                )
            
            return PHONE
            
        except Exception as e:
            logger.error(f"Error starting registration: {e}")
            return ConversationHandler.END
    
    async def language_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection during registration"""
        try:
            query = update.callback_query
            user = update.effective_user
            user_id = user.id
            language_code = query.data.split('_')[-1]
            logger.info(f"User {user_id} (@{user.username}) register started with language: {language_code}")
            
            # Validate language code
            if language_code not in config.localization.supported_languages:
                await query.answer("❌ Invalid language selection")
                return SELECT_LANGUAGE  # Stay in language selection state
            
            user_repo = BotUserRepository(db_manager)

            existing_user = await user_repo.get_user_by_telegram_id(user_id)
            logger.info(f"existing_user {existing_user} for user_id {user_id}")

            if not existing_user:
                try:
                    async with api_client as client:
                        registration_data = {
                            'first_name': user.first_name,
                            'last_name': user.last_name,
                            'username': user.username,
                            'language_code': language_code
                        }
                        response = await client.register_telegram_user(user_id, registration_data)
                        if not response.success:
                            logger.error(f"Failed to register telegram user {user_id}: {response.error}")
                            await query.answer("❌ Registration failed")
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text="❌ Registration failed. Please try again with /start or contact support."
                            )
                            return ConversationHandler.END
                except Exception as e:
                    logger.error(f"Exception during telegram user registration: {e}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    await query.answer("❌ Registration failed")
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ Registration failed. Please try again with /start."
                    )
                    return ConversationHandler.END
            else:
                # Update user's preferred language
                await self.user_repo.update_user_language(user_id, language_code)
                await query.answer("✅ Language updated")
            
            # Proceed to phone number input
            phone_text = i18n.get('telegram.registration.enter_phone', language_code)
            keyboard = ProfileKeyboards.phone_request(language_code)
            
            # Send the phone request message first
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=phone_text,
                reply_markup=keyboard
            )
            
            # Then try to delete the old language selection message
            try:
                await query.delete_message()
            except Exception as del_error:
                logger.warning(f"Could not delete language selection message: {del_error}")
            
            return PHONE
            
        except Exception as e:
            logger.error(f"Error in language selection: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def phone_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number from contact - checks for duplicates and offers linking"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            contact = update.message.contact

            if contact.user_id != user_id:
                await update.message.reply_text(
                    "❌ Please share your own contact information.",
                    reply_markup=ReplyKeyboardRemove()
                )
                return PHONE

            phone = normalize_phone_number(contact.phone_number)
            
            # Check if phone is available via API
            try:
                async with api_client as client:
                    response = await client.check_phone_availability(user_id, phone)
                    
                    # Extract nested data - API returns {'data': {...}, 'success': True}
                    response_data = response.data.get('data', {}) if response.data else {}
                    
                    if response.success and response_data.get('available'):
                        # Phone is available - save it normally
                        await self.user_repo.set_user_phone(user_id, phone)
                        
                        # Registration complete
                        complete_text = i18n.get('telegram.registration_complete', language)
                        keyboard = MenuKeyboards.main_menu(language)
                        
                        await update.message.reply_text(
                            text=complete_text,
                            reply_markup=keyboard
                        )
                        
                        logger.info(f"Registration completed for user {user_id}")
                        return ConversationHandler.END
                    
                    elif response.success and not response_data.get('available'):
                        # Phone exists - check if linking is possible
                        available = response_data.get('available', False)
                        can_link = response_data.get('can_link', False)
                        existing_user = response_data.get('existing_user_masked', {})
                        
                        logger.info(f"Phone check for user {user_id}: available={available}, can_link={can_link}, existing_user={existing_user}")
                        
                        if can_link:
                            # Store phone for linking
                            context.user_data['pending_link_phone'] = phone
                            
                            # Show linking option
                            masked_name = existing_user.get('name', '***') if existing_user else '***'
                            
                            link_text = (
                                f"📱 This phone number is already registered to an account ({masked_name}).\n\n"
                                f"Would you like to link your Telegram to this existing account?\n"
                                f"This will merge your accounts."
                            )
                            
                            keyboard = KeyboardBuilder.build_inline_keyboard([
                                [{'text': "✅ Yes, link accounts", 'callback_data': "link_yes"}],
                                [{'text': "❌ No, use different phone", 'callback_data': "link_no"}]
                            ])
                            
                            await update.message.reply_text(
                                text=link_text,
                                reply_markup=keyboard
                            )
                            
                            return LINK_ACCOUNT_CONFIRM
                        else:
                            # Cannot link - phone belongs to another telegram user
                            await update.message.reply_text(
                                "❌ This phone number is already linked to another Telegram account.\n"
                                "Please use a different phone number.",
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE
                    else:
                        # API error
                        logger.error(f"Failed to check phone availability: {response.error}")
                        await update.message.reply_text(
                            "❌ Unable to verify phone. Please try again.",
                            reply_markup=ProfileKeyboards.phone_request(language)
                        )
                        return PHONE
                        
            except Exception as api_error:
                logger.error(f"API error checking phone: {api_error}")
                # Fall back to direct save (will fail if duplicate, which is caught below)
                await self.user_repo.set_user_phone(user_id, phone)
                
                complete_text = i18n.get('telegram.registration_complete', language)
                keyboard = MenuKeyboards.main_menu(language)
                
                await update.message.reply_text(
                    text=complete_text,
                    reply_markup=keyboard
                )
                
                logger.info(f"Registration completed for user {user_id}")
                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error handling phone: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            
            # Check if it's a duplicate key error
            if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                language = await i18n.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    "❌ This phone number is already registered.\n"
                    "Please use a different phone number or contact support.",
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE
            
            return ConversationHandler.END
    
    async def phone_text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number as text"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            phone_text = update.message.text.strip()

            if not await validate_phone_number(phone_text):
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language)
                )
                return PHONE

            phone = normalize_phone_number(phone_text)

            # Check if phone is available via API
            try:
                async with api_client as client:
                    response = await client.check_phone_availability(user_id, phone)
                    
                    # Extract nested data - API returns {'data': {...}, 'success': True}
                    response_data = response.data.get('data', {}) if response.data else {}
                    
                    if response.success and response_data.get('available'):
                        # Phone is available - save it normally
                        await self.user_repo.set_user_phone(user_id, phone)
                        
                        # Remove the share contact keyboard first
                        await update.message.reply_text(
                            i18n.get('telegram.phone.phone_accepted', language) or "✅ Phone number accepted",
                            reply_markup=ReplyKeyboardRemove()
                        )

                        # Registration complete
                        complete_text = i18n.get('telegram.registration_complete', language)
                        keyboard = MenuKeyboards.main_menu(language)
                        
                        await update.message.reply_text(
                            text=complete_text,
                            reply_markup=keyboard
                        )
                        
                        logger.info(f"Registration completed for user {user_id}")
                        return ConversationHandler.END
                    
                    elif response.success and not response_data.get('available'):
                        # Phone exists - check if linking is possible
                        available = response_data.get('available', False)
                        can_link = response_data.get('can_link', False)
                        existing_user = response_data.get('existing_user_masked', {})
                        
                        logger.info(f"Phone check for user {user_id}: available={available}, can_link={can_link}, existing_user={existing_user}")
                        
                        if can_link:
                            # Store phone for linking
                            context.user_data['pending_link_phone'] = phone
                            
                            # Remove the share contact keyboard first
                            await update.message.reply_text(
                                i18n.get('telegram.phone.phone_accepted', language) or "✅ Phone number accepted",
                                reply_markup=ReplyKeyboardRemove()
                            )

                            # Show linking option
                            masked_name = existing_user.get('name', '***') if existing_user else '***'
                            
                            link_text = (
                                f"📱 This phone number is already registered to an account ({masked_name}).\n\n"
                                f"Would you like to link your Telegram to this existing account?\n"
                                f"This will merge your accounts."
                            )
                            
                            keyboard = KeyboardBuilder.build_inline_keyboard([
                                [{'text': "✅ Yes, link accounts", 'callback_data': "link_yes"}],
                                [{'text': "❌ No, use different phone", 'callback_data': "link_no"}]
                            ])
                            
                            await update.message.reply_text(
                                text=link_text,
                                reply_markup=keyboard
                            )
                            
                            return LINK_ACCOUNT_CONFIRM
                        else:
                            # Cannot link - phone belongs to another telegram user
                            await update.message.reply_text(
                                "❌ This phone number is already linked to another Telegram account.\n"
                                "Please use a different phone number.",
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE
                    else:
                        # API error
                        logger.error(f"Failed to check phone availability: {response.error}")
                        await update.message.reply_text(
                            "❌ Unable to verify phone. Please try again.",
                            reply_markup=ProfileKeyboards.phone_request(language)
                        )
                        return PHONE
                        
            except Exception as api_error:
                logger.error(f"API error checking phone: {api_error}")
                # Fall back to direct save (will fail if duplicate, which is caught below)
                await self.user_repo.set_user_phone(user_id, phone)
                
                # Remove the share contact keyboard first
                await update.message.reply_text(
                    i18n.get('telegram.phone.phone_accepted', language) or "✅ Phone number accepted",
                    reply_markup=ReplyKeyboardRemove()
                )

                complete_text = i18n.get('telegram.registration_complete', language)
                keyboard = MenuKeyboards.main_menu(language)
                
                await update.message.reply_text(
                    text=complete_text,
                    reply_markup=keyboard
                )
                
                logger.info(f"Registration completed for user {user_id}")
                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error handling phone text: {e}")
            
            # Check if it's a duplicate key error
            if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                language = await i18n.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    "❌ This phone number is already registered.\n"
                    "Please use a different phone number or contact support.",
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE
            
            return ConversationHandler.END
    
    async def link_account_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user's choice to link or cancel account linking"""
        try:
            query = update.callback_query
            await query.answer()
            
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            callback_data = query.data
            
            if callback_data == "link_yes":
                # User wants to link - send OTP
                phone = context.user_data.get('pending_link_phone')
                
                if not phone:
                    await query.edit_message_text(
                        "❌ Session expired. Please share your phone number again.",
                        reply_markup=None
                    )
                    return PHONE
                
                # Call API to send OTP
                try:
                    async with api_client as client:
                        response = await client.link_phone_send_otp(user_id, phone)
                        
                        if response.success:
                            phone_masked = response.data.get('phone_masked', phone)
                            await query.edit_message_text(
                                f"📱 A verification code has been sent to {phone_masked}.\n\n"
                                f"Please enter the 6-digit code:",
                                reply_markup=None
                            )
                            return LINK_ACCOUNT_OTP
                        else:
                            error_msg = response.error or "Failed to send verification code"
                            await query.edit_message_text(
                                f"❌ {error_msg}\n\nPlease try again or use a different phone.",
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE
                            
                except Exception as api_error:
                    logger.error(f"API error sending OTP: {api_error}")
                    await query.edit_message_text(
                        "❌ Failed to send verification code. Please try again.",
                        reply_markup=None
                    )
                    return PHONE
                    
            elif callback_data == "link_no":
                # User wants to use different phone
                context.user_data.pop('pending_link_phone', None)
                
                await query.edit_message_text(
                    "📱 Please share a different phone number:",
                    reply_markup=None
                )
                
                # Send keyboard for phone sharing
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Share your phone number using the button below:",
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                
                return PHONE
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Error in link_account_confirm: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def link_account_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle OTP verification for account linking"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            otp = update.message.text.strip()
            
            # Validate OTP format
            if not otp.isdigit() or len(otp) != 6:
                await update.message.reply_text(
                    "❌ Please enter a valid 6-digit code."
                )
                return LINK_ACCOUNT_OTP
            
            phone = context.user_data.get('pending_link_phone')
            if not phone:
                await update.message.reply_text(
                    "❌ Session expired. Please start again with /start"
                )
                return ConversationHandler.END
            
            # Call API to verify OTP and link accounts
            try:
                async with api_client as client:
                    response = await client.link_phone_verify(user_id, otp)
                    
                    if response.success:
                        # Account linked successfully!
                        context.user_data.pop('pending_link_phone', None)
                        
                        user_data = response.data.get('user', {})
                        name = user_data.get('first_name', 'User')
                        
                        await update.message.reply_text(
                            f"✅ Accounts linked successfully!\n\n"
                            f"Welcome back, {name}! Your Telegram is now connected to your existing account.",
                            reply_markup=MenuKeyboards.main_menu(language)
                        )
                        
                        logger.info(f"Account linking completed for user {user_id}")
                        return ConversationHandler.END
                    else:
                        error_msg = response.error or "Invalid verification code"
                        
                        # Check if it's an expired/invalid OTP
                        if 'expired' in error_msg.lower() or 'not found' in error_msg.lower():
                            await update.message.reply_text(
                                "❌ Verification code expired. Please start again with /start"
                            )
                            return ConversationHandler.END
                        else:
                            await update.message.reply_text(
                                f"❌ {error_msg}\n\nPlease try again:"
                            )
                            return LINK_ACCOUNT_OTP
                        
            except Exception as api_error:
                logger.error(f"API error verifying OTP: {api_error}")
                await update.message.reply_text(
                    "❌ Verification failed. Please try again:"
                )
                return LINK_ACCOUNT_OTP
            
        except Exception as e:
            logger.error(f"Error in link_account_otp: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def name_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle name input or OTP verification"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            text = update.message.text.strip()

            # Check if we're waiting for OTP verification
            if context.user_data.get('awaiting_otp'):
                # Validate OTP format (6 digits)
                if not text.isdigit() or len(text) != 6:
                    await update.message.reply_text(
                        i18n.get('telegram.phone.otp_invalid', language)
                    )
                    return NAME

                # Verify OTP via API
                try:
                    async with api_client as client:
                        user_token = await get_auth_token(update, context, client)
                        if user_token:
                            response = await client.verify_phone_otp(user_token, text)
                            if response.success:
                                await update.message.reply_text(
                                    i18n.get('telegram.phone.otp_success', language),
                                    parse_mode='Markdown'
                                )

                                # Clear OTP flags
                                context.user_data.pop('awaiting_otp', None)
                                context.user_data.pop('pending_phone_verification', None)

                                logger.info(f"Phone verification successful for user {user_id}")

                                # Now ask for name
                                name_text = i18n.get('telegram.enter_name', language)
                                await update.message.reply_text(name_text)

                                return NAME
                            else:
                                await update.message.reply_text(
                                    f"❌ Verification failed: {response.error}\n\n"
                                    "Please enter the correct code or /cancel to skip:"
                                )
                                return NAME
                except Exception as verify_error:
                    logger.error(f"Error verifying OTP: {verify_error}")
                    await update.message.reply_text(
                        "❌ Verification failed. Please try again or /cancel to skip."
                    )
                    return NAME

            # Handle name input
            name = text

            if len(name) < 2:
                await update.message.reply_text("❌ Name is too short. Please enter your full name.")
                return NAME

            # Update user profile
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    profile_data = {
                        'first_name': name.split()[0] if name.split() else name,
                        'last_name': ' '.join(name.split()[1:]) if len(name.split()) > 1 else ''
                    }
                    await client.update_user_profile(user_token, profile_data)

            # Registration complete
            complete_text = i18n.get('telegram.registration_complete', language)
            keyboard = MenuKeyboards.main_menu(language)

            await update.message.reply_text(
                text=complete_text,
                reply_markup=keyboard
            )

            logger.info(f"Registration completed for user {user_id}")

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error handling name: {e}")
            return ConversationHandler.END
    
    async def continue_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Continue registration after phone sharing"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Check if user already has full profile
            user_data = await self.user_repo.get_user_by_telegram_id(user_id)
            if user_data and user_data.get('full_name'):
                # Already registered, show main menu
                await self.profile_menu(update, context)
                return
            
            # Ask for name
            name_text = i18n.get('telegram.registration.enter_name', language)
            await update.message.reply_text(name_text)
            
        except Exception as e:
            logger.error(f"Error continuing registration: {e}")
    
    async def cancel_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel registration process"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            cancel_text = i18n.get('telegram.action_cancelled', language)
            keyboard = MenuKeyboards.main_menu(language)
            
            await update.message.reply_text(
                text=cancel_text,
                reply_markup=keyboard
            )
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Error canceling registration: {e}")
            return ConversationHandler.END
    
    async def edit_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle profile editing"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            edit_text = f"{i18n.get('telegram.edit_profile', language)}\n\nWhat would you like to update?\n\nType the new information or use /cancel to go back."
            
            await query.edit_message_text(
                text=edit_text,
                reply_markup=MenuKeyboards.cancel_button(language)
            )
            await query.answer()
            
            # Set user state for profile editing
            await self.user_repo.update_user_state(user_id, {'awaiting_input': 'profile_edit'})
            
        except Exception as e:
            logger.error(f"Error in edit profile: {e}")
            await self._handle_error(update)
    
    async def manage_addresses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address management"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Clear any pending input state
            await self.user_repo.update_user_state(user_id, {})
            
            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
            
            if not addresses:
                addresses_text = i18n.get('telegram.address.no_addresses', language)
                keyboard = ProfileKeyboards.empty_addresses(language)
                logger.info(f"No addresses found, showing empty addresses keyboard")
            else:
                addresses_text = f"📍 Your Addresses ({len(addresses)}):\n\n"
                for i, addr in enumerate(addresses, 1):
                    status = "🏠" if addr.get('is_default') else "📍"
                    addresses_text += f"{status} {addr.get('title', f'Address {i}')}\n"
                    addresses_text += f"   {addr.get('full_address', 'No address')}\n\n"
                
                # Create proper address management keyboard
                keyboard = ProfileKeyboards.addresses_management(addresses, language)
                logger.info(f"Found {len(addresses)} addresses, showing management keyboard")
            
            await query.edit_message_text(
                text=addresses_text,
                reply_markup=keyboard
            )
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error managing addresses: {e}")
            await self._handle_error(update)
    
    async def add_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start address adding process - entry point for enhanced address flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Clear any pending database state before starting address flow
            await self.user_repo.update_user_state(user_id, {})

            logger.info(f"=== ADD ADDRESS CONVERSATION ENTRY POINT ===")
            logger.info(f"User: {user_id}")
            if update.callback_query:
                logger.info(f"Callback data: {update.callback_query.data}")
            logger.info(f"Starting add address conversation for user {user_id}")

            # Initialize temp address data
            context.user_data['temp_address_data'] = {}
            context.user_data['conversation_state'] = 'address_location'
            logger.info(f"Set conversation state to: address_location")

            # Use enhanced location request with skip option
            location_text = i18n.get('telegram.address.location_prompt_enhanced', language) or (
                "📍 *Add New Address*\n\n"
                "Please share your location for accurate delivery, "
                "or enter your address manually.\n\n"
                "Sharing location is recommended for precise delivery."
            )
            keyboard = ProfileKeyboards.location_request_with_skip(language)

            if update.callback_query:
                logger.info(f"Editing message via callback query")
                await update.callback_query.delete_message()
                await update.callback_query.answer()
                # Send keyboard in new message
                await update.callback_query.message.reply_text(
                    text=location_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
                logger.info(f"Callback query processed and keyboard sent")
            else:
                logger.info(f"Replying to message directly")
                await update.message.reply_text(
                    text=location_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

            logger.info(f"Address conversation started, returning ADDRESS_LOCATION state ({ADDRESS_LOCATION})")
            return ADDRESS_LOCATION

        except Exception as e:
            logger.error(f"Error starting add address: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def location_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle location sharing for address - primary path with reverse geocoding"""
        logger.info(f"=== LOCATION_RECEIVED METHOD CALLED ===")

        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not update.message or not update.message.location:
                logger.error(f"ERROR: No location in message!")
                return ConversationHandler.END

            location = update.message.location
            logger.info(f"Location received: lat={location.latitude}, lng={location.longitude}")

            # Store location in temp address data
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}

            context.user_data['temp_address_data']['latitude'] = location.latitude
            context.user_data['temp_address_data']['longitude'] = location.longitude
            context.user_data['temp_address_data']['location_source'] = 'shared'

            # Attempt reverse geocoding
            reverse_geocoded_address = None
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.reverse_geocode(user_token, location.latitude, location.longitude)
                    if response.success and response.data.get('data'):
                        reverse_geocoded_address = response.data['data'].get('formatted_address')
                        context.user_data['temp_address_data']['full_address'] = reverse_geocoded_address
                        logger.info(f"Reverse geocoded address: {reverse_geocoded_address}")

            # Remove reply keyboard
            await update.message.reply_text(
                "📍 Location received!",
                reply_markup=ReplyKeyboardRemove()
            )

            # Ask for address title with suggestions
            title_prompt = i18n.get('telegram.address.title_prompt', language) or (
                "Great! Now give this address a name.\n\n"
                "You can choose from the suggestions below or type your own:"
            )
            if reverse_geocoded_address:
                title_prompt = f"📍 *Detected location:*\n{reverse_geocoded_address}\n\n" + title_prompt

            keyboard = ProfileKeyboards.address_title_suggestions(language)

            await update.message.reply_text(
                title_prompt,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

            logger.info(f"Transitioning to ADDRESS_TITLE state")
            return ADDRESS_TITLE

        except Exception as e:
            logger.error(f"CRITICAL ERROR in location_received: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def address_text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address as text"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            address_text = update.message.text.strip()
            
            # Store address text temporarily
            context.user_data['temp_address'] = address_text
            
            await update.message.reply_text(
                i18n.get('telegram.address.title_received', language),
                reply_markup=ReplyKeyboardRemove()
            )
            
            return ADDRESS_TITLE
            
        except Exception as e:
            logger.error(f"Error handling address text: {e}")
            return ConversationHandler.END
    
    async def address_title_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address title from text input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            title = update.message.text.strip()

            logger.info(f"User {user_id} entered title: {title}")

            # Store title in temp address data
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['title'] = title

            # Check if this is from location sharing flow (has coordinates already)
            addr_data = context.user_data['temp_address_data']
            if addr_data.get('latitude') and addr_data.get('longitude'):
                # Location already set, ask for delivery instructions
                instructions_prompt = i18n.get('telegram.address.enter_delivery_instructions', language) or (
                    "Any special delivery instructions?\n"
                    "(e.g., door code, call before arriving)\n\n"
                    "Type your instructions or click Skip:"
                )
                keyboard = ProfileKeyboards.delivery_instructions_keyboard(language)

                await update.message.reply_text(
                    instructions_prompt,
                    reply_markup=keyboard
                )

                return ADDRESS_DELIVERY_INSTRUCTIONS
            else:
                # Legacy flow - save directly
                # Prepare address data
                address_data = {
                    'title': title,
                    'full_address': context.user_data.get('temp_address', 'Location-based address')
                }

                # Add coordinates if available from old flow
                if 'temp_location' in context.user_data:
                    loc = context.user_data['temp_location']
                    address_data['latitude'] = loc['latitude']
                    address_data['longitude'] = loc['longitude']

                # Save address via API
                async with api_client as client:
                    user_token = await get_auth_token(update, context, client)
                    if user_token:
                        response = await client.add_user_address(user_token, address_data)
                        if response.success:
                            success_text = f"✅ Address '{title}' added successfully!"
                        else:
                            success_text = "❌ Failed to add address. Please try again."
                    else:
                        success_text = "❌ Authentication failed."

                keyboard = MenuKeyboards.main_menu(language)
                await update.message.reply_text(
                    text=success_text,
                    reply_markup=keyboard
                )

                # Clear temporary data
                context.user_data.pop('temp_location', None)
                context.user_data.pop('temp_address', None)
                context.user_data.pop('temp_address_data', None)

                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error handling address title: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def cancel_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address adding process"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)
            keyboard = MenuKeyboards.main_menu(language)

            # Handle both message and callback query
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )

            # Clear all temporary address data
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)
            context.user_data.pop('temp_address_data', None)

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling address: {e}")
            return ConversationHandler.END

    async def cancel_address_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address adding from text button (removes ReplyKeyboard)"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)
            
            # First remove the reply keyboard
            await update.message.reply_text(
                "❌ Cancelled",
                reply_markup=ReplyKeyboardRemove()
            )
            
            # Then show main menu
            keyboard = MenuKeyboards.main_menu(language)
            await update.message.reply_text(
                text=cancel_text,
                reply_markup=keyboard
            )

            # Clear all temporary address data
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)
            context.user_data.pop('temp_address_data', None)

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling address from text: {e}")
            return ConversationHandler.END

    # ==================== MANUAL ADDRESS ENTRY HANDLERS ====================

    async def skip_location_sharing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle skip location - start manual entry flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            logger.info(f"User {user_id} chose manual address entry")

            # Initialize temp address data if not exists
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['location_source'] = 'manual'

            # Remove reply keyboard
            await update.message.reply_text(
                "✏️ Manual address entry",
                reply_markup=ReplyKeyboardRemove()
            )

            # Show region selection (only Tashkent for now)
            region_prompt = i18n.get('telegram.address.select_region', language) or (
                "Please select your region:"
            )
            keyboard = ProfileKeyboards.region_selection(language)

            await update.message.reply_text(
                region_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_REGION

        except Exception as e:
            logger.error(f"Error in skip_location_sharing: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def region_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle region selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract region from callback data
            region = query.data.replace('region_', '')
            logger.info(f"User {user_id} selected region: {region}")

            # Store region
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['region'] = region
            context.user_data['temp_address_data']['city'] = 'Tashkent'

            await query.answer()

            # Show district selection
            district_prompt = i18n.get('telegram.address.select_district', language) or (
                "Please select your district:"
            )
            districts = get_all_districts(language)
            keyboard = ProfileKeyboards.district_selection(districts, language)

            await query.edit_message_text(
                district_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_DISTRICT

        except Exception as e:
            logger.error(f"Error in region_selected: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def back_to_region(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle back button from district selection - go back to region selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await query.answer()
            logger.info(f"User {user_id} going back to region selection")

            # Show region selection again
            region_prompt = i18n.get('telegram.address.select_region', language) or "Please select your region:"
            keyboard = ProfileKeyboards.region_selection(language)

            await query.edit_message_text(
                region_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_REGION

        except Exception as e:
            logger.error(f"Error in back_to_region: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def district_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle district selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract district from callback data
            district_key = query.data.replace('district_', '')
            district_name = get_district_name(district_key, language)
            logger.info(f"User {user_id} selected district: {district_key} ({district_name})")

            # Store district
            context.user_data['temp_address_data']['district'] = district_key
            context.user_data['temp_address_data']['district_name'] = district_name

            # Get district center for geocoding hint
            center = get_district_center(district_key)
            context.user_data['temp_address_data']['hint_lat'] = center[0]
            context.user_data['temp_address_data']['hint_lon'] = center[1]

            await query.answer()

            # Ask for street name (required, no skip option)
            street_prompt = escape_markdown(i18n.get('telegram.address.enter_street_required', language), version=2) or (
                f"📍 District: *{escape_markdown(district_name, version=2)}*\n\n"
                "🛤️ Please enter your street name (required):"
            )
            # No skip keyboard - street is required

            await query.edit_message_text(
                street_prompt,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )

            return ADDRESS_STREET

        except Exception as e:
            logger.error(f"Error in district_selected: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def street_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle street name input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            street = update.message.text.strip()

            logger.info(f"User {user_id} entered street: {street}")
            context.user_data['temp_address_data']['street_address'] = street

            # Ask for building number
            building_prompt = i18n.get('telegram.address.enter_building', language) or (
                "Please enter your building/house number, or skip:"
            )
            keyboard = ProfileKeyboards.optional_field_keyboard('building', language)

            await update.message.reply_text(
                building_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_BUILDING

        except Exception as e:
            logger.error(f"Error in street_received: {e}")
            return ConversationHandler.END

    async def building_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle building number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            building = update.message.text.strip()

            logger.info(f"User {user_id} entered building: {building}")
            context.user_data['temp_address_data']['building_number'] = building

            # Ask for apartment number
            apartment_prompt = i18n.get('telegram.address.enter_apartment', language) or (
                "Please enter your apartment number, or skip:"
            )
            keyboard = ProfileKeyboards.optional_field_keyboard('apartment', language)

            await update.message.reply_text(
                apartment_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_APARTMENT

        except Exception as e:
            logger.error(f"Error in building_received: {e}")
            return ConversationHandler.END

    async def apartment_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle apartment number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            apartment = update.message.text.strip()

            logger.info(f"User {user_id} entered apartment: {apartment}")
            context.user_data['temp_address_data']['apartment_number'] = apartment

            # Ask for floor number
            floor_prompt = i18n.get('telegram.address.enter_floor', language) or (
                "Please enter your floor number, or skip:"
            )
            keyboard = ProfileKeyboards.optional_field_keyboard('floor', language)

            await update.message.reply_text(
                floor_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_FLOOR

        except Exception as e:
            logger.error(f"Error in apartment_received: {e}")
            return ConversationHandler.END

    async def floor_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle floor number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            floor = update.message.text.strip()

            logger.info(f"User {user_id} entered floor: {floor}")
            context.user_data['temp_address_data']['floor_number'] = floor

            # Ask for entrance number
            entrance_prompt = i18n.get('telegram.address.enter_entrance', language) or (
                "Please enter your entrance/podyezd number, or skip:"
            )
            keyboard = ProfileKeyboards.optional_field_keyboard('entrance', language)

            await update.message.reply_text(
                entrance_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_ENTRANCE

        except Exception as e:
            logger.error(f"Error in floor_received: {e}")
            return ConversationHandler.END

    async def entrance_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle entrance number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            entrance = update.message.text.strip()

            logger.info(f"User {user_id} entered entrance: {entrance}")
            context.user_data['temp_address_data']['entrance'] = entrance

            # Ask for delivery instructions
            instructions_prompt = i18n.get('telegram.address.enter_delivery_instructions', language) or (
                "Any special delivery instructions?\n"
                "(e.g., door code, call before arriving, preferred delivery times)\n\n"
                "Or skip if none:"
            )
            keyboard = ProfileKeyboards.delivery_instructions_keyboard(language)

            await update.message.reply_text(
                instructions_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_DELIVERY_INSTRUCTIONS

        except Exception as e:
            logger.error(f"Error in entrance_received: {e}")
            return ConversationHandler.END

    async def delivery_instructions_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle delivery instructions input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            instructions = update.message.text.strip()

            logger.info(f"User {user_id} entered delivery instructions")
            context.user_data['temp_address_data']['delivery_instructions'] = instructions

            # Check if we already have coordinates from location sharing
            addr_data = context.user_data.get('temp_address_data', {})
            if addr_data.get('latitude') and addr_data.get('longitude') and addr_data.get('location_source') == 'shared':
                # Location was shared - save directly without geocoding
                logger.info(f"Location already set from sharing, saving address directly")
                return await self.save_address_final(update, context)
            else:
                # Manual entry flow - proceed to geocoding and confirmation
                return await self.geocode_and_confirm(update, context)

        except Exception as e:
            logger.error(f"Error in delivery_instructions_received: {e}")
            return ConversationHandler.END

    async def skip_field_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle skip button for optional fields"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract field name from callback data
            field_name = query.data.replace('skip_', '')
            logger.info(f"User {user_id} skipped field: {field_name}")

            await query.answer()

            # Determine next state based on skipped field
            # Note: Street field is no longer skippable - it's required
            if field_name == 'street':
                # This case should not happen anymore since street has no skip button
                # But keep it here for safety - redirect to building
                logger.warning(f"Street skip attempted but street is required")
                building_prompt = i18n.get('telegram.address.enter_building', language) or (
                    "Please enter your building/house number, or skip:"
                )
                keyboard = ProfileKeyboards.optional_field_keyboard('building', language)
                await query.edit_message_text(building_prompt, reply_markup=keyboard)
                return ADDRESS_BUILDING

            elif field_name == 'building':
                # Skip to delivery instructions
                instructions_prompt = i18n.get('telegram.address.enter_delivery_instructions', language) or (
                    "Any special delivery instructions?\n"
                    "(e.g., door code, call before arriving)\n\n"
                    "Or skip if none:"
                )
                keyboard = ProfileKeyboards.delivery_instructions_keyboard(language)
                await query.edit_message_text(instructions_prompt, reply_markup=keyboard)
                return ADDRESS_DELIVERY_INSTRUCTIONS

            elif field_name == 'apartment':
                floor_prompt = i18n.get('telegram.address.enter_floor', language) or "Please enter your floor number, or skip:"
                keyboard = ProfileKeyboards.optional_field_keyboard('floor', language)
                await query.edit_message_text(floor_prompt, reply_markup=keyboard)
                return ADDRESS_FLOOR

            elif field_name == 'floor':
                entrance_prompt = i18n.get('telegram.address.enter_entrance', language) or "Please enter your entrance number, or skip:"
                keyboard = ProfileKeyboards.optional_field_keyboard('entrance', language)
                await query.edit_message_text(entrance_prompt, reply_markup=keyboard)
                return ADDRESS_ENTRANCE

            elif field_name == 'entrance':
                instructions_prompt = i18n.get('telegram.address.enter_delivery_instructions', language) or (
                    "Any special delivery instructions?\n"
                    "(e.g., door code, call before arriving)\n\n"
                    "Or skip if none:"
                )
                keyboard = ProfileKeyboards.delivery_instructions_keyboard(language)
                await query.edit_message_text(instructions_prompt, reply_markup=keyboard)
                return ADDRESS_DELIVERY_INSTRUCTIONS

            elif field_name == 'delivery_instructions':
                # Check if we already have coordinates from location sharing
                addr_data = context.user_data.get('temp_address_data', {})
                if addr_data.get('latitude') and addr_data.get('longitude') and addr_data.get('location_source') == 'shared':
                    # Location was shared - save directly without geocoding
                    logger.info(f"Location already set from sharing, saving address directly")
                    return await self.save_address_final(update, context, is_callback=True)
                else:
                    # Manual entry flow - proceed to geocoding
                    return await self.geocode_and_confirm_callback(update, context)

            else:
                logger.warning(f"Unknown field skipped: {field_name}")
                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in skip_field_handler: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def geocode_and_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Geocode the manual address and show confirmation"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            addr_data = context.user_data.get('temp_address_data', {})

            # Build address string for geocoding
            address_parts = []
            if addr_data.get('street_address'):
                address_parts.append(f"{addr_data['street_address']} street")
            if addr_data.get('building_number'):
                address_parts.append(addr_data['building_number'])
            if addr_data.get('district_name'):
                address_parts.append(addr_data['district_name'])
            address_parts.append('Tashkent, Uzbekistan')

            address_string = ', '.join(address_parts)
            logger.info(f"Geocoding address: {address_string}")

            # Attempt geocoding
            geocode_success = False
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    hint_lat = addr_data.get('hint_lat')
                    hint_lon = addr_data.get('hint_lon')

                    response = await client.geocode_address(
                        user_token, address_string, hint_lat, hint_lon
                    )

                    if response.success and response.data.get('data'):
                        geo_data = response.data['data']
                        addr_data['latitude'] = geo_data.get('latitude')
                        addr_data['longitude'] = geo_data.get('longitude')
                        addr_data['full_address'] = geo_data.get('formatted_address', address_string)
                        geocode_success = True
                        logger.info(f"Geocoding successful: {addr_data['latitude']}, {addr_data['longitude']}")

            # If geocoding failed, use district center as fallback
            if not geocode_success:
                logger.warning(f"Geocoding failed, using district center as fallback")
                district_key = addr_data.get('district', 'yunusabad')
                center = get_district_center(district_key)
                addr_data['latitude'] = center[0]
                addr_data['longitude'] = center[1]
                addr_data['full_address'] = address_string

            context.user_data['temp_address_data'] = addr_data

            # Send location pin for confirmation
            await update.message.reply_location(
                latitude=addr_data['latitude'],
                longitude=addr_data['longitude']
            )

            # Show confirmation message
            confirm_text = i18n.get('telegram.address.geocode_found', language) or (
                "📍 *Location Found*\n\n"
                f"Address: {addr_data.get('full_address', 'N/A')}\n\n"
                "Is this location correct?"
            )
            if not geocode_success:
                confirm_text += "\n\n⚠️ _Note: Exact location could not be determined. Using approximate district center._"

            keyboard = ProfileKeyboards.geocode_confirmation(language, show_edit=False)

            await update.message.reply_text(
                confirm_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

            return ADDRESS_GEOCODE_CONFIRM

        except Exception as e:
            logger.error(f"Error in geocode_and_confirm: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def geocode_and_confirm_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Geocode and confirm from callback query (skip button)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            addr_data = context.user_data.get('temp_address_data', {})

            await query.answer("Processing...")

            # Build address string
            address_parts = []
            if addr_data.get('street_address'):
                address_parts.append(f"{addr_data['street_address']} street")
            if addr_data.get('building_number'):
                address_parts.append(addr_data['building_number'])
            if addr_data.get('district_name'):
                address_parts.append(addr_data['district_name'])
            address_parts.append('Tashkent, Uzbekistan')

            address_string = ', '.join(address_parts)
            logger.info(f"Geocoding address: {address_string}")

            # Attempt geocoding
            geocode_success = False
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    hint_lat = addr_data.get('hint_lat')
                    hint_lon = addr_data.get('hint_lon')

                    response = await client.geocode_address(
                        user_token, address_string, hint_lat, hint_lon
                    )

                    if response.success and response.data.get('data'):
                        geo_data = response.data['data']
                        addr_data['latitude'] = geo_data.get('latitude')
                        addr_data['longitude'] = geo_data.get('longitude')
                        addr_data['full_address'] = geo_data.get('formatted_address', address_string)
                        geocode_success = True

            # Fallback to district center
            if not geocode_success:
                district_key = addr_data.get('district', 'yunusabad')
                center = get_district_center(district_key)
                addr_data['latitude'] = center[0]
                addr_data['longitude'] = center[1]
                addr_data['full_address'] = address_string

            context.user_data['temp_address_data'] = addr_data

            # Delete old message and send location
            await query.delete_message()

            await query.message.reply_location(
                latitude=addr_data['latitude'],
                longitude=addr_data['longitude']
            )

            confirm_text = i18n.get('telegram.address.geocode_found', language) or (
                "📍 *Location Found*\n\n"
                f"Address: {addr_data.get('full_address', 'N/A')}\n\n"
                "Is this location correct?"
            )
            if not geocode_success:
                confirm_text += "\n\n⚠️ _Note: Using approximate district center location._"

            keyboard = ProfileKeyboards.geocode_confirmation(language, show_edit=False)

            await query.message.reply_text(
                confirm_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )

            return ADDRESS_GEOCODE_CONFIRM

        except Exception as e:
            logger.error(f"Error in geocode_and_confirm_callback: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def confirm_geocode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User confirms the geocoded location - proceed to title"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await query.answer("✅ Location confirmed!")
            logger.info(f"User {user_id} confirmed geocoded location")

            # Ask for address title
            title_prompt = i18n.get('telegram.address.title_prompt', language) or (
                "Great! Now give this address a name.\n\n"
                "You can choose from the suggestions below or type your own:"
            )
            keyboard = ProfileKeyboards.address_title_suggestions(language)

            await query.edit_message_text(
                title_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_TITLE

        except Exception as e:
            logger.error(f"Error in confirm_geocode: {e}")
            return ConversationHandler.END

    async def retry_geocode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User says location is wrong - offer to share location or re-enter manually"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await query.answer("Let's fix the location!")
            logger.info(f"User {user_id} says geocode is wrong, offering correction options")

            # Delete previous message with inline keyboard
            await query.delete_message()

            # Keep temp address data but reset for potential location share
            if 'temp_address_data' in context.user_data:
                context.user_data['temp_address_data']['location_source'] = 'retry'

            # Offer location sharing or manual re-entry
            retry_text = i18n.get('telegram.address.retry_location', language) or (
                "📍 Let's fix the location\n\n"
                "Please share your exact location for accurate delivery,\n"
                "or click 'Re-enter Address' to try again manually."
            )
            
            keyboard = ProfileKeyboards.location_request_with_retry(language)

            await query.message.reply_text(
                retry_text,
                reply_markup=keyboard
            )

            return ADDRESS_LOCATION  # Go back to location state to handle shared location

        except Exception as e:
            logger.error(f"Error in retry_geocode: {e}")
            return ConversationHandler.END

    async def address_title_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address title from callback (suggestions)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract title from callback
            title_key = query.data.replace('addr_title_', '')
            titles = {
                'home': {'en': 'Home', 'uz': 'Uy', 'ru': 'Дом'},
                'work': {'en': 'Work', 'uz': 'Ish', 'ru': 'Работа'},
                'other': {'en': 'Other', 'uz': 'Boshqa', 'ru': 'Другое'}
            }
            title = titles.get(title_key, {}).get(language, title_key.capitalize())

            logger.info(f"User {user_id} selected title: {title}")
            context.user_data['temp_address_data']['title'] = title

            await query.answer()

            # Save the address
            return await self.save_address_final(update, context, is_callback=True)

        except Exception as e:
            logger.error(f"Error in address_title_callback: {e}")
            return ConversationHandler.END

    async def save_address_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool = False):
        """Save the address to API"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            addr_data = context.user_data.get('temp_address_data', {})

            # Prepare address data for API
            address_payload = {
                'title': addr_data.get('title', 'My Address'),
                'full_address': addr_data.get('full_address', ''),
                'street_address': addr_data.get('street_address'),
                'city': addr_data.get('city', 'Tashkent'),
                'district': addr_data.get('district'),
                'latitude': addr_data.get('latitude'),
                'longitude': addr_data.get('longitude'),
                'apartment_number': addr_data.get('apartment_number'),
                'floor_number': addr_data.get('floor_number'),
                'delivery_instructions': addr_data.get('delivery_instructions'),
            }

            # Remove None values
            address_payload = {k: v for k, v in address_payload.items() if v is not None}

            logger.info(f"Saving address for user {user_id}: {address_payload}")

            # Save via API
            success = False
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.add_user_address(user_token, address_payload)
                    if response.success:
                        success = True
                        logger.info(f"Address saved successfully for user {user_id}")
                    else:
                        logger.error(f"Failed to save address: {response.error}")

            # Clear temp data
            context.user_data.pop('temp_address_data', None)
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)

            if success:
                success_text = i18n.get('telegram.address.saved_successfully', language) or (
                    f"✅ Address '{addr_data.get('title', 'My Address')}' saved successfully!"
                )
            else:
                success_text = "❌ Failed to save address. Please try again."

            keyboard = MenuKeyboards.main_menu(language)

            if is_callback:
                query = update.callback_query
                await query.edit_message_text(
                    text=success_text,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=success_text,
                    reply_markup=keyboard
                )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in save_address_final: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END
    
    async def view_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View specific address details"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Clear any pending input state (user may have cancelled an edit)
            await self.user_repo.update_user_state(user_id, {})
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Get address details from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
                address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)
                
                if not address:
                    await query.answer("Address not found")
                    return
            
            # Format address details
            address_text = f"📍 **{address.get('title', 'Untitled Address')}**\n\n"
            address_text += f"**Full Address:** {address.get('full_address', 'N/A')}\n"
            if address.get('street_address'):
                address_text += f"**Street:** {address.get('street_address')}\n"
            if address.get('city'):
                address_text += f"**City:** {address.get('city')}\n"
            if address.get('is_default'):
                address_text += f"\n🏠 **Default Address**\n"
            
            # Create action buttons for this address
            buttons = [
                [
                    {'text': '✏️ Edit', 'callback_data': f'edit_address_{address_id}'},
                    {'text': '🗑️ Delete', 'callback_data': f'delete_address_{address_id}'}
                ]
            ]
            
            if not address.get('is_default'):
                buttons.insert(0, [{'text': '🏠 Set as Default', 'callback_data': f'set_default_address_{address_id}'}])
            
            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            try:
                await query.edit_message_text(
                    text=address_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
            except BadRequest as edit_error:
                # Handle "message is not modified" error
                if "message is not modified" in str(edit_error).lower():
                    logger.info(f"Message content unchanged for address {address_id}")
                else:
                    raise edit_error
            
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error viewing address: {e}")
            await self._handle_error(update)
    
    async def select_edit_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show address selection for editing"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
            
            if not addresses:
                await query.answer("No addresses to edit")
                return
            
            edit_text = "✏️ **Select address to edit:**\n\nClick on the address you want to modify:"
            
            # Create selection buttons
            buttons = []
            for addr in addresses:
                status = "🏠" if addr.get('is_default') else "📍"
                addr_title = addr.get('title', f"Address {addr.get('id')}")
                buttons.append([{
                    'text': f"{status} {addr_title}",
                    'callback_data': f"edit_address_{addr['id']}"
                }])
            
            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            await query.edit_message_text(
                text=edit_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in select edit address: {e}")
            await self._handle_error(update)
    
    async def select_delete_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show address selection for deletion"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
            
            if not addresses:
                await query.answer("No addresses to delete")
                return
            
            delete_text = "🗑️ **Select address to delete:**\n\n⚠️ **Warning:** This action cannot be undone!"
            
            # Create selection buttons
            buttons = []
            for addr in addresses:
                status = "🏠" if addr.get('is_default') else "📍"
                addr_title = addr.get('title', f"Address {addr.get('id')}")
                buttons.append([{
                    'text': f"{status} {addr_title}",
                    'callback_data': f"confirm_delete_address_{addr['id']}"
                }])
            
            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            await query.edit_message_text(
                text=delete_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in select delete address: {e}")
            await self._handle_error(update)
    
    async def set_default_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set address as default"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Set address as default via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Call the API to set address as default
                response = await client.set_default_address(user_token, int(address_id))
                if response.success:
                    await query.answer("✅ Address set as default!")
                    logger.info(f"Address {address_id} successfully set as default")
                    
                    # Refresh the address view to show updated status
                    await self.view_address(update, context)
                else:
                    await query.answer(f"❌ Failed to set as default: {response.error}")
                    logger.error(f"Failed to set address {address_id} as default: {response.error}")
            
        except Exception as e:
            logger.error(f"Error setting default address: {e}")
            await self._handle_error(update)
    
    async def edit_address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing specific address"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Clear any old pending input state before showing edit menu
            await self.user_repo.update_user_state(user_id, {})
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Show editing options for the address
            edit_text = "✏️ **Edit Address Options:**\n\n"
            edit_text += "Choose what you'd like to edit about this address:\n\n"
            edit_text += "💡 **Quick tip:** For major changes, you can delete this address and add a new one."
            
            # Create editing options buttons
            buttons = [
                [
                    {'text': '📝 Edit Title', 'callback_data': f'edit_title_{address_id}'},
                    {'text': '📍 Edit Location', 'callback_data': f'edit_location_{address_id}'}
                ],
                [
                    {'text': '📋 Edit Details', 'callback_data': f'edit_details_{address_id}'},
                    {'text': '📞 Edit Instructions', 'callback_data': f'edit_instructions_{address_id}'}
                ],
                [
                    {'text': '🗑️ Delete & Re-add', 'callback_data': f'delete_address_{address_id}'},
                    {'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}
                ]
            ]
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            try:
                await query.edit_message_text(
                    text=edit_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
            except BadRequest as edit_error:
                if "message is not modified" not in str(edit_error).lower():
                    raise edit_error
            
            await query.answer()
            logger.info(f"Address editing options shown for address {address_id}")
            
        except Exception as e:
            logger.error(f"Error in edit address handler: {e}")
            await self._handle_error(update)
    
    async def delete_address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address deletion confirmation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID from callback data  
            address_id = query.data.split('_')[-1]
            
            # Get address details for confirmation
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
                address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)
                
                if not address:
                    await query.answer("Address not found")
                    return
            
            # Show confirmation dialog
            confirm_text = i18n.get('telegram.address.delete_confirmation', language, title=address.get('title', 'Untitled'), address=address.get('full_address', 'N/A'))
            
            buttons = [
                [
                    {'text': '✅ Yes, Delete', 'callback_data': f'confirm_delete_address_{address_id}'},
                    {'text': '❌ Cancel', 'callback_data': f'view_address_{address_id}'}
                ]
            ]
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            await query.edit_message_text(
                text=confirm_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in delete address handler: {e}")
            await self._handle_error(update)
    
    async def confirm_delete_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute address deletion"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Delete address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Call the API to delete the address
                response = await client.delete_user_address(user_token, int(address_id))
                if response.success:
                    await query.answer("🗑️ Address deleted successfully!")
                    logger.info(f"Address {address_id} successfully deleted")
                    
                    # Redirect back to address management
                    await self.manage_addresses(update, context)
                else:
                    await query.answer(f"❌ Failed to delete address: {response.error}")
                    logger.error(f"Failed to delete address {address_id}: {response.error}")
                    
                    # Show error and go back to address view
                    error_text = f"❌ **Error deleting address:**\n\n{response.error}\n\nPlease try again."
                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]
                    
                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)
                    
                    try:
                        await query.edit_message_text(
                            text=error_text,
                            reply_markup=keyboard,
                            parse_mode='Markdown'
                        )
                    except BadRequest as edit_error:
                        if "message is not modified" not in str(edit_error).lower():
                            raise edit_error
            
        except Exception as e:
            logger.error(f"Error confirming delete address: {e}")
            await self._handle_error(update)
    
    async def edit_title_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address title"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Store address ID for conversation
            context.user_data['edit_address_id'] = address_id
            context.user_data['edit_field'] = 'title'
            
            # Get current address details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if response.success:
                    addresses = response.data.get('data', {}).get('addresses', [])
                    address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)
                    
                    if address:
                        current_title = address.get('title', 'Untitled')
                        edit_text = f"📝 **Edit Address Title**\n\n"
                        edit_text += f"**Current title:** {current_title}\n\n"
                        edit_text += f"Please type the new title for this address:"
                        
                        cancel_button = [[{'text': '❌ Cancel', 'callback_data': f'view_address_{address_id}'}]]
                        from keyboards import KeyboardBuilder
                        keyboard = KeyboardBuilder.build_inline_keyboard(cancel_button)
                        
                        await query.edit_message_text(
                            text=edit_text,
                            reply_markup=keyboard,
                            parse_mode='Markdown'
                        )
                        await query.answer()
                        
                        # Set state to wait for title input
                        await self.user_repo.update_user_state(user_id, {
                            'awaiting_input': 'edit_address_title',
                            'edit_address_id': address_id
                        })
                        
                        return
            
            await query.answer("❌ Address not found")
            
        except Exception as e:
            logger.error(f"Error in edit title handler: {e}")
            await self._handle_error(update)
    
    async def edit_location_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address location"""
        try:
            query = update.callback_query
            address_id = query.data.split('_')[-1]
            
            await query.answer("📍 Location editing: Please delete and re-add the address with the new location for now.")
            logger.info(f"Location edit requested for address {address_id} - redirecting to delete/add flow")
            
        except Exception as e:
            logger.error(f"Error in edit location handler: {e}")
            await self._handle_error(update)
    
    async def edit_details_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address details"""
        try:
            query = update.callback_query
            address_id = query.data.split('_')[-1]
            
            await query.answer("📋 Address details editing will be available in the next update!")
            logger.info(f"Details edit requested for address {address_id} - not yet implemented")
            
        except Exception as e:
            logger.error(f"Error in edit details handler: {e}")
            await self._handle_error(update)
    
    async def edit_instructions_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing delivery instructions"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]
            
            # Get current address details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if response.success:
                    addresses = response.data.get('data', {}).get('addresses', [])
                    address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)
                    
                    if address:
                        current_instructions = address.get('delivery_instructions') or 'None'
                        edit_text = f"📞 **Edit Delivery Instructions**\n\n"
                        edit_text += f"**Current instructions:** {current_instructions}\n\n"
                        edit_text += f"Please type the new delivery instructions for this address:"
                        
                        cancel_button = [[{'text': '❌ Cancel', 'callback_data': f'view_address_{address_id}'}]]
                        from keyboards import KeyboardBuilder
                        keyboard = KeyboardBuilder.build_inline_keyboard(cancel_button)
                        
                        await query.edit_message_text(
                            text=edit_text,
                            reply_markup=keyboard,
                            parse_mode='Markdown'
                        )
                        await query.answer()
                        
                        # Set state to wait for instructions input
                        await self.user_repo.update_user_state(user_id, {
                            'awaiting_input': 'edit_address_instructions',
                            'edit_address_id': address_id
                        })
                        
                        return
            
            await query.answer("❌ Address not found")
            
        except Exception as e:
            logger.error(f"Error in edit instructions handler: {e}")
            await self._handle_error(update)
    
    async def handle_address_title_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                      text: str, user_state: Dict):
        """Handle address title editing input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            address_id = user_state.get('edit_address_id')
            
            if not address_id:
                await update.message.reply_text("❌ Address editing session expired. Please try again.")
                await self.user_repo.update_user_state(user_id, {})
                return
            
            # Validate title input
            if len(text.strip()) < 2:
                await update.message.reply_text("❌ Title is too short. Please enter at least 2 characters.")
                return
            
            if len(text.strip()) > 50:
                await update.message.reply_text("❌ Title is too long. Please keep it under 50 characters.")
                return
            
            # Update address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Prepare update data with just the title
                update_data = {'title': text.strip()}
                
                response = await client.update_user_address(user_token, int(address_id), update_data)
                if response.success:
                    success_text = f"✅ **Address title updated successfully!**\n\n"
                    success_text += f"**New title:** {text.strip()}"
                    
                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]
                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)
                    
                    await update.message.reply_text(
                        text=success_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                    
                    # Clear user state
                    await self.user_repo.update_user_state(user_id, {})
                    logger.info(f"Address {address_id} title updated to: {text.strip()}")
                    
                else:
                    error_text = f"❌ **Failed to update address title:**\n\n{response.error}\n\nPlease try again."
                    await update.message.reply_text(error_text, parse_mode='Markdown')
                    logger.error(f"Failed to update address {address_id} title: {response.error}")
            
        except Exception as e:
            logger.error(f"Error handling address title edit: {e}")
            await update.message.reply_text("❌ An error occurred while updating the address title. Please try again.")
    
    async def handle_address_instructions_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                             text: str, user_state: Dict):
        """Handle address delivery instructions editing input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            address_id = user_state.get('edit_address_id')
            
            if not address_id:
                await update.message.reply_text("❌ Address editing session expired. Please try again.")
                await self.user_repo.update_user_state(user_id, {})
                return
            
            # Validate instructions input
            if len(text.strip()) > 200:
                await update.message.reply_text("❌ Instructions are too long. Please keep them under 200 characters.")
                return
            
            # Update address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Prepare update data with delivery instructions
                update_data = {'delivery_instructions': text.strip()}
                
                response = await client.update_user_address(user_token, int(address_id), update_data)
                if response.success:
                    success_text = f"📞 **Delivery instructions updated successfully!**\n\n"
                    if text.strip():
                        success_text += f"**New instructions:** {text.strip()}"
                    else:
                        success_text += f"**Instructions:** None (cleared)"
                    
                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]
                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)
                    
                    await update.message.reply_text(
                        text=success_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                    
                    # Clear user state
                    await self.user_repo.update_user_state(user_id, {})
                    logger.info(f"Address {address_id} delivery instructions updated")
                    
                else:
                    error_text = f"❌ **Failed to update delivery instructions:**\n\n{response.error}\n\nPlease try again."
                    await update.message.reply_text(error_text, parse_mode='Markdown')
                    logger.error(f"Failed to update address {address_id} instructions: {response.error}")
            
        except Exception as e:
            logger.error(f"Error handling address instructions edit: {e}")
            await update.message.reply_text("❌ An error occurred while updating delivery instructions. Please try again.")
    
    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error"""
        error_msg = i18n.get('telegram.error.auth_failed', language)

        if update.callback_query:
            await update.callback_query.edit_message_text(error_msg)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(error_msg)
    
    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error"""
        error_msg = f"❌ {error}"
        
        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)
    
    async def logout_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user logout from all platforms"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Confirm logout action
            logout_text = f"🚪 **{i18n.get('telegram.profile.logout_confirm', language, 'Are you sure you want to logout?')}**\n\n"
            logout_text += f"This will log you out from both Telegram bot and web app.\n\n"
            logout_text += f"You can always log back in by using /start"
            
            buttons = [
                [
                    {'text': '✅ Yes, Logout', 'callback_data': 'confirm_logout'},
                    {'text': '❌ Cancel', 'callback_data': 'profile_menu'}
                ]
            ]
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=logout_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=logout_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
            
            logger.info(f"Logout confirmation shown to user {user_id}")
            
        except Exception as e:
            logger.error(f"Error in logout handler: {e}")
            await self._handle_error(update)
    
    async def confirm_logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute logout"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Call logout API to invalidate tokens
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    try:
                        # Call logout-all endpoint to invalidate all sessions
                        await client.logout_all_sessions(user_token)
                        logger.info(f"Successfully logged out user {user_id} from all sessions")
                    except Exception as api_error:
                        logger.warning(f"API logout failed for user {user_id}: {api_error}")
                        # Continue with local logout even if API fails
            
            # Clear local bot user data
            await self.user_repo.clear_user_session(user_id)
            
            # Show logout success message
            logout_success = f"🚪 **Logged out successfully!**\n\n"
            logout_success += f"You have been logged out from all platforms.\n\n"
            logout_success += f"To log back in, use the /start command."
            
            # Remove inline keyboard
            await query.edit_message_text(
                text=logout_success,
                parse_mode='Markdown'
            )
            await query.answer("✅ Logged out successfully!")
            
            logger.info(f"User {user_id} successfully logged out")
            
        except Exception as e:
            logger.error(f"Error confirming logout: {e}")
            await self._handle_error(update)
    
    async def _handle_error(self, update: Update):
        """Handle general error"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('telegram.error_occurred', language)
        except:
            error_msg = i18n.get('telegram.error_occurred', 'en')

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)


# Global handler instance
profile_handlers = ProfileHandlers()

# Export conversation states
profile_handlers.SELECT_LANGUAGE = SELECT_LANGUAGE
profile_handlers.PHONE = PHONE
profile_handlers.NAME = NAME
profile_handlers.ADDRESS_LOCATION = ADDRESS_LOCATION
profile_handlers.ADDRESS_TITLE = ADDRESS_TITLE
profile_handlers.ADDRESS_REGION = ADDRESS_REGION
profile_handlers.ADDRESS_DISTRICT = ADDRESS_DISTRICT
profile_handlers.ADDRESS_STREET = ADDRESS_STREET
profile_handlers.ADDRESS_BUILDING = ADDRESS_BUILDING
profile_handlers.ADDRESS_APARTMENT = ADDRESS_APARTMENT
profile_handlers.ADDRESS_FLOOR = ADDRESS_FLOOR
profile_handlers.ADDRESS_ENTRANCE = ADDRESS_ENTRANCE
profile_handlers.ADDRESS_DELIVERY_INSTRUCTIONS = ADDRESS_DELIVERY_INSTRUCTIONS
profile_handlers.ADDRESS_GEOCODE_CONFIRM = ADDRESS_GEOCODE_CONFIRM
profile_handlers.PHONE_VERIFY_PHONE = PHONE_VERIFY_PHONE
profile_handlers.PHONE_VERIFY_NAME = PHONE_VERIFY_NAME