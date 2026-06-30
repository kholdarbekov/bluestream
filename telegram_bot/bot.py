"""
Main Telegram Bot Application
Comprehensive water business bot with full feature integration
"""
import asyncio
import logging
import signal
import sys
import os
from datetime import datetime
from typing import Optional, Dict, Any
import json

# Initialize Sentry before any other imports to capture all errors
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_scrub import before_send as _sentry_before_send

# Initialize Sentry if DSN is configured
_sentry_dsn = os.environ.get('SENTRY_DSN')
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get('SENTRY_ENVIRONMENT', os.environ.get('FLASK_ENV', 'development')),
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.05')),
        profiles_sample_rate=float(os.environ.get('SENTRY_PROFILES_SAMPLE_RATE', '0.01')),
        send_default_pii=os.environ.get('SENTRY_SEND_DEFAULT_PII', 'false').lower() == 'true',
        debug=os.environ.get('SENTRY_DEBUG', 'false').lower() == 'true',
        integrations=[
            AsyncioIntegration(),
        ],
        # Set release version if available
        release=os.environ.get('APP_VERSION', 'telegram-bot@1.0.0'),
        before_send=_sentry_before_send,
    )

from telegram import Update, BotCommand, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, TypeHandler
)
from telegram.error import TelegramError
from shared.telegram_request import ResilientHTTPXRequest

# Setup logging first
from logging_config import setup_logging, log_bot_startup_info

# Setup logging configuration
log_level = os.getenv('LOG_LEVEL', 'INFO')
setup_logging(log_level=log_level, log_to_file=False)

# Bot modules
from config import config
from database import db_manager, BotUserRepository
from i18n import i18n
from api_client import api_client
from webhook_server import webhook_server
from token_manager import TokenManager
from handlers import (
    main_menu_handler, language_handler,
    product_handlers, order_handlers, subscription_handlers,
    profile_handlers, loyalty_handlers, admin_handlers,
    support_handlers, payment_handlers, bottle_handlers,
    quick_order_handlers,
)
# Import conversation states directly (they are module-level constants)
from handlers.profile import (
    SELECT_LANGUAGE, PHONE, LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP,
    ADDRESS_LOCATION, ADDRESS_TITLE, ADDRESS_REGION, ADDRESS_DISTRICT,
    ADDRESS_STREET, ADDRESS_BUILDING, ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_ENTRANCE, ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM
)
from eligibility import main_menu_for
from utils import error_handler, rate_limiter, user_middleware, get_auth_token
from keyboards import MenuKeyboards

logger = logging.getLogger('bot')


class WaterBusinessBot:
    """Main bot application class"""

    def __init__(self):
        self.application: Optional[Application] = None
        self.is_running = False
        self.user_repository = BotUserRepository(db_manager)
        self.token_manager: Optional[TokenManager] = None

    async def initialize(self):
        """Initialize bot and all dependencies"""
        try:
            log_bot_startup_info()
            logger.info("Initializing Water Business Bot...")

            # Initialize database connection
            await db_manager.connect()

            # Load translations
            await i18n.load_translations()

            # Initialize API client
            # Note: api_client will be used as async context manager in handlers

            # Initialize TokenManager for JWT token caching
            logger.info("Initializing TokenManager...")
            self.token_manager = TokenManager(config.redis.url)
            if await self.token_manager.connect():
                logger.info("TokenManager connected to Redis successfully")
            else:
                logger.warning("TokenManager running without Redis - tokens will not be cached")

            # Build Telegram application
            logger.info("Creating Telegram Application...")
            request = ResilientHTTPXRequest(
                connection_pool_size=config.telegram.request_connection_pool_size,
                connect_timeout=config.telegram.request_connect_timeout,
                read_timeout=config.telegram.request_read_timeout,
                write_timeout=config.telegram.request_write_timeout,
                pool_timeout=config.telegram.request_pool_timeout,
                max_retries=config.telegram.request_max_retries,
                retry_backoff_seconds=config.telegram.request_retry_backoff_seconds,
                retry_max_backoff_seconds=config.telegram.request_retry_max_backoff_seconds,
                http_version='1.1',
            )

            # Keep read timeout above long-poll timeout to avoid client-side premature timeouts.
            get_updates_read_timeout = max(
                config.telegram.get_updates_read_timeout,
                float(config.telegram.polling_timeout + 5),
            )
            get_updates_request = ResilientHTTPXRequest(
                connection_pool_size=config.telegram.get_updates_connection_pool_size,
                connect_timeout=config.telegram.get_updates_connect_timeout,
                read_timeout=get_updates_read_timeout,
                write_timeout=config.telegram.get_updates_write_timeout,
                pool_timeout=config.telegram.get_updates_pool_timeout,
                max_retries=config.telegram.get_updates_max_retries,
                retry_backoff_seconds=config.telegram.get_updates_retry_backoff_seconds,
                retry_max_backoff_seconds=config.telegram.get_updates_retry_max_backoff_seconds,
                http_version='1.1',
            )

            self.application = (
                Application.builder()
                .token(config.telegram.bot_token)
                .request(request)
                .get_updates_request(get_updates_request)
                .build()
            )
            logger.info("Telegram Application created successfully!")

            # Start webhook server for bot management
            logger.info("Starting webhook server...")
            webhook_server.set_application(self.application)
            await webhook_server.start()
            logger.info("Webhook server started successfully")

            # Test bot connection
            logger.info("Testing bot connection to Telegram...")
            try:
                bot_info = await self.application.bot.get_me()
                logger.info(f"Bot connected successfully! Bot info: @{bot_info.username} ({bot_info.first_name})")
            except Exception as bot_error:
                logger.error(f"Failed to connect to Telegram: {bot_error}")
                raise

            # Store token_manager in bot_data for handler access
            self.application.bot_data['token_manager'] = self.token_manager

            # Set up handlers
            logger.info("Setting up handlers...")
            await self._setup_handlers()
            logger.info("Handlers setup completed!")

            # Set up bot commands
            logger.info("Setting up bot commands...")
            await self._setup_bot_commands()
            logger.info("Bot commands setup completed!")

            # Set up error handling
            self.application.add_error_handler(error_handler)
            logger.info("Error handler setup completed!")

            # Add middleware to log updates (minimal in production, detailed in DEBUG)
            async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
                import logging as _logging
                user_id = update.effective_user.id if update.effective_user else 'N/A'

                if not logger.isEnabledFor(_logging.DEBUG):
                    # Production: log minimal info only, no sensitive data
                    update_type = 'message'
                    if update.message and update.message.successful_payment:
                        update_type = 'successful_payment'
                    elif update.callback_query:
                        update_type = 'callback_query'
                    elif update.pre_checkout_query:
                        update_type = 'pre_checkout_query'
                    elif update.edited_message:
                        update_type = 'edited_message'
                    logger.info(f"Update received: type={update_type}, user={user_id}")
                    return

                # DEBUG level: log full details with sensitive data redacted
                logger.debug(f"UPDATE RECEIVED: type={type(update).__name__}")
                if update.message:
                    if update.message.successful_payment:
                        sp = update.message.successful_payment
                        logger.debug(
                            f"SUCCESSFUL_PAYMENT - User: {user_id}, "
                            f"Currency: {sp.currency}, "
                            f"Telegram charge ID: {sp.telegram_payment_charge_id[:8]}..."
                        )
                    else:
                        logger.debug(f"Message from user {user_id}, text: {update.message.text[:50] if update.message.text else 'NO TEXT'}")
                if update.callback_query:
                    logger.debug(f"CALLBACK QUERY: {update.callback_query.data} from user {user_id}")
                if update.pre_checkout_query:
                    logger.debug(
                        f"PRE_CHECKOUT_QUERY - User: {user_id}, "
                        f"Currency: {update.pre_checkout_query.currency}"
                    )


            self.application.add_handler(TypeHandler(Update, log_all_updates), group=-10)
            logger.info("Update logging middleware installed!")

            # Callback-dedup middleware: raises ApplicationHandlerStop on
            # duplicates within a short TTL, answering ONLY the dropped
            # duplicate (handlers own the ack for taps they process — a
            # middleware pre-answer would consume Telegram's single
            # answerCallbackQuery slot and hide handler error toasts). Sits
            # between the debug logger (group=-10, sees everything including
            # duplicates) and the conversation/main handlers (group ≥ -2,
            # never see duplicates). Root-cause fix for the production
            # "Message to edit/delete not found" warning pair caused by
            # double-taps on inline buttons. See handlers.callback_dedup.
            from handlers.callback_dedup import callback_dedup_middleware
            self.application.add_handler(
                TypeHandler(Update, callback_dedup_middleware), group=-5
            )
            logger.info("Callback dedup middleware installed!")

            logger.info("Bot initialization completed successfully")

        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def _setup_handlers(self):
        """Set up all bot handlers"""

        # Command handlers
        self.application.add_handler(CommandHandler("menu", main_menu_handler))
        self.application.add_handler(CommandHandler("help", support_handlers.help_handler))
        self.application.add_handler(CommandHandler("language", language_handler.language_menu))

        # Admin commands (restricted - access control handled in handler)
        self.application.add_handler(CommandHandler("admin", admin_handlers.admin_panel))

        # Callback query handlers
        callback_handlers = [
            # Main menu callbacks
            CallbackQueryHandler(main_menu_handler, pattern="^back_to_main$"),
            CallbackQueryHandler(language_handler.language_menu, pattern="^menu_language$"),
            CallbackQueryHandler(language_handler.set_language, pattern="^set_language_"),

            # Product callbacks
            CallbackQueryHandler(product_handlers.products_menu, pattern="^menu_products$"),
            CallbackQueryHandler(product_handlers.products_menu, pattern="^back_to_categories$"),
            CallbackQueryHandler(product_handlers.category_handler, pattern="^category_"),
            CallbackQueryHandler(product_handlers.product_details, pattern="^product_"),
            CallbackQueryHandler(product_handlers.product_details, pattern="^back_to_product_"),
            CallbackQueryHandler(product_handlers.add_to_cart, pattern="^add_to_cart_"),
            CallbackQueryHandler(product_handlers.quantity_handler, pattern="^qty_"),
            CallbackQueryHandler(product_handlers.cart_handler, pattern="^cart_"),
            CallbackQueryHandler(product_handlers.show_cart, pattern="^back_to_cart$"),

            # Quick Order callbacks (Products menu top + order-history reorder)
            CallbackQueryHandler(quick_order_handlers.handle_repeat_last, pattern="^quick_repeat_last$"),
            CallbackQueryHandler(quick_order_handlers.handle_usual, pattern="^quick_usual$"),
            CallbackQueryHandler(quick_order_handlers.handle_reorder_from_history, pattern="^reorder_\\d+$"),

            # Order callbacks
            CallbackQueryHandler(order_handlers.orders_menu, pattern="^menu_orders$"),
            CallbackQueryHandler(order_handlers.order_details, pattern="^order_"),
            # Reward-in-checkout handlers MUST precede the broad "^checkout" catch-all
            # below, or it would swallow these checkout_* callbacks.
            CallbackQueryHandler(order_handlers.checkout_choose_reward, pattern="^checkout_choose_reward$"),
            CallbackQueryHandler(order_handlers.checkout_apply_reward, pattern="^checkout_apply_reward_\\d+$"),
            CallbackQueryHandler(order_handlers.checkout_remove_reward, pattern="^checkout_remove_reward$"),
            CallbackQueryHandler(order_handlers.back_to_payment, pattern="^back_to_payment$"),
            CallbackQueryHandler(order_handlers.checkout_handler, pattern="^checkout"),
            CallbackQueryHandler(order_handlers.address_handler, pattern="^address_"),
            CallbackQueryHandler(order_handlers.payment_handler, pattern="^payment_(cash|card|payme|click|uzcard|humo|business_account)$"),
            CallbackQueryHandler(order_handlers.confirm_order, pattern="^confirm_order"),
            CallbackQueryHandler(order_handlers.select_payment_cash, pattern="^select_payment_cash$"),
            CallbackQueryHandler(order_handlers.cancel_checkout, pattern="^cancel_order$"),
            CallbackQueryHandler(order_handlers.edit_cart, pattern="^edit_order$"),
            CallbackQueryHandler(order_handlers.back_to_order_confirm, pattern="^back_to_order_confirm$"),
            CallbackQueryHandler(order_handlers.track_order, pattern="^track_order_"),
            CallbackQueryHandler(order_handlers.orders_menu, pattern="^back_to_orders$"),
            CallbackQueryHandler(order_handlers.cancel_order, pattern="^cancel_order_\\d+"),
            # Back to delivery address selection (from payment method screen)
            CallbackQueryHandler(order_handlers.checkout_handler, pattern="^back_to_delivery$"),

            # Confirmation callbacks
            # Order Cancellation
            CallbackQueryHandler(order_handlers.cancel_order_confirm_yes, pattern="^cancel_order_confirm_yes$"),
            CallbackQueryHandler(order_handlers.cancel_order_confirm_no, pattern="^cancel_order_confirm_no$"),

            # Subscription callbacks
            CallbackQueryHandler(subscription_handlers.subscriptions_menu, pattern="^menu_subscriptions$"),
            CallbackQueryHandler(subscription_handlers.subscriptions_menu, pattern="^back_to_subscriptions$"),
            CallbackQueryHandler(subscription_handlers.subscription_details, pattern="^subscription_\\d+$"),
            CallbackQueryHandler(subscription_handlers.subscription_actions, pattern="^(pause|resume|cancel)_sub_"),
            CallbackQueryHandler(subscription_handlers.skip_delivery, pattern="^skip_sub_"),
            CallbackQueryHandler(subscription_handlers.view_billing_history, pattern="^billing_history_"),

            # Subscription item management
            CallbackQueryHandler(subscription_handlers.manage_subscription_items, pattern="^manage_items_"),
            CallbackQueryHandler(subscription_handlers.remove_item_confirm, pattern="^remove_item_"),

            # Subscription editing
            CallbackQueryHandler(subscription_handlers.edit_subscription_menu, pattern="^edit_sub_"),
            CallbackQueryHandler(subscription_handlers.change_frequency, pattern="^change_frequency_"),
            CallbackQueryHandler(subscription_handlers.change_payment_method_menu, pattern="^change_payment_"),

            # Statistics and logs
            CallbackQueryHandler(subscription_handlers.view_subscription_statistics, pattern="^subscription_statistics$"),
            CallbackQueryHandler(subscription_handlers.view_subscription_logs, pattern="^view_logs_"),
            CallbackQueryHandler(subscription_handlers.retry_failed_billing, pattern="^retry_billing_"),

            # Profile callbacks
            CallbackQueryHandler(profile_handlers.profile_menu, pattern="^menu_profile$"),
            CallbackQueryHandler(profile_handlers.phone_verification_menu, pattern="^phone_verification$"),
            CallbackQueryHandler(profile_handlers.notification_settings, pattern="^notification_settings$"),
            CallbackQueryHandler(
                profile_handlers.toggle_delivery_telegram_status_notifications,
                pattern="^toggle_delivery_telegram_status_(on|off)$",
            ),
            # add_phone_number is now handled by phone_verification_handler ConversationHandler
            CallbackQueryHandler(profile_handlers.verify_phone_number, pattern="^verify_phone_number$"),
            CallbackQueryHandler(profile_handlers.edit_profile, pattern="^edit_profile$"),
            # Profile field-edit sub-menu (Deliverable C)
            CallbackQueryHandler(profile_handlers.edit_profile_name_prompt, pattern="^edit_profile_name$"),
            CallbackQueryHandler(profile_handlers.edit_profile_birthday_start, pattern="^edit_profile_birthday$"),
            CallbackQueryHandler(language_handler.language_menu, pattern="^edit_profile_language$"),
            CallbackQueryHandler(profile_handlers.phone_verification_menu, pattern="^edit_profile_phone$"),
            # cancel_action used by the name-edit prompt / birthday prompt -> return to profile
            CallbackQueryHandler(profile_handlers.profile_menu, pattern="^cancel_action$"),
            CallbackQueryHandler(profile_handlers.manage_addresses, pattern="^manage_addresses$"),
            CallbackQueryHandler(profile_handlers.view_address, pattern="^view_address_"),
            CallbackQueryHandler(profile_handlers.select_edit_address, pattern="^select_edit_address$"),
            CallbackQueryHandler(profile_handlers.select_delete_address, pattern="^select_delete_address$"),
            # Address action callbacks
            CallbackQueryHandler(profile_handlers.set_default_address, pattern="^set_default_address_"),
            CallbackQueryHandler(profile_handlers.edit_address_handler, pattern="^edit_address_"),
            CallbackQueryHandler(profile_handlers.delete_address_handler, pattern="^delete_address_"),
            CallbackQueryHandler(profile_handlers.confirm_delete_address, pattern="^confirm_delete_address_"),
            # Address editing callbacks
            CallbackQueryHandler(profile_handlers.edit_title_handler, pattern="^edit_title_"),
            CallbackQueryHandler(profile_handlers.edit_location_handler, pattern="^edit_location_"),
            CallbackQueryHandler(profile_handlers.edit_details_handler, pattern="^edit_details_"),
            CallbackQueryHandler(profile_handlers.edit_instructions_handler, pattern="^edit_instructions_"),

            # Bottle balance callback
            CallbackQueryHandler(bottle_handlers.show_bottle_balance, pattern="^my_bottles$"),

            # Loyalty callbacks
            CallbackQueryHandler(loyalty_handlers.loyalty_menu, pattern="^menu_loyalty$"),
            CallbackQueryHandler(loyalty_handlers.loyalty_history, pattern=r"^loyalty_history(_page_\d+)?$"),
            CallbackQueryHandler(loyalty_handlers.loyalty_rewards, pattern="^loyalty_rewards$"),
            CallbackQueryHandler(loyalty_handlers.loyalty_referral, pattern="^loyalty_referral$"),
            CallbackQueryHandler(loyalty_handlers.redeem_reward, pattern="^redeem_"),

            # Support callbacks
            CallbackQueryHandler(support_handlers.support_menu, pattern="^menu_support$"),
            CallbackQueryHandler(support_handlers.faq_handler, pattern="^faq$"),
            CallbackQueryHandler(support_handlers.contact_support, pattern="^contact_support$"),

            # Admin callbacks (restricted - access control handled in handler)
            CallbackQueryHandler(admin_handlers.admin_orders, pattern="^admin_orders$"),
            CallbackQueryHandler(admin_handlers.admin_analytics, pattern="^admin_analytics$"),

            # Payment callbacks
            CallbackQueryHandler(payment_handlers.retry_payment, pattern="^payment_retry_"),
            CallbackQueryHandler(payment_handlers.switch_payment_method, pattern="^payment_switch_"),
            CallbackQueryHandler(payment_handlers.cancel_payment, pattern="^payment_cancel_"),
        ]


        # Add logging callback handler to catch all callbacks for debugging
        async def debug_callback_handler(update, context):
            if update.callback_query:
                logger.debug(
                    "Callback received: user=%s, data=%s, message_id=%s",
                    update.effective_user.id,
                    update.callback_query.data,
                    update.callback_query.message.message_id,
                )
                # Don't process, just log and let other handlers handle it

        # Add debug handler that catches all callbacks but doesn't interfere
        self.application.add_handler(CallbackQueryHandler(debug_callback_handler), group=-1)

        # Add all callback handlers
        for handler in callback_handlers:
            self.application.add_handler(handler)

        # Conversation handlers for complex flows
        registration_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", profile_handlers.start_registration_new),
                # CallbackQueryHandler(profile_handlers.start_registration_new, pattern="^/start$")
            ],
            states={
                SELECT_LANGUAGE: [
                    CallbackQueryHandler(profile_handlers.language_selection, pattern="^set_language_")
                ],
                PHONE: [
                    MessageHandler(filters.CONTACT, profile_handlers.phone_received),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.phone_text_received)
                ],
                # Account linking states
                LINK_ACCOUNT_CONFIRM: [
                    CallbackQueryHandler(profile_handlers.link_account_confirm, pattern="^link_")
                ],
                LINK_ACCOUNT_OTP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.link_account_otp)
                ],
                # Registration OTP captured in-conversation (Task 12). /cancel
                # and /start fallbacks below now apply during OTP entry.
                REGISTER_OTP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.register_otp_received)
                ],
            },
            fallbacks=[
                CommandHandler("start", profile_handlers.start_registration_new),
                CommandHandler("cancel", profile_handlers.cancel_registration),
            ],
            per_chat=True,
            per_user=True,
            name="registration",
            conversation_timeout=300,  # 5 minutes timeout
            allow_reentry=True
        )
        self.application.add_handler(registration_handler, group=-2)

        # Address input conversation - Enhanced flow with manual entry support
        address_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(profile_handlers.add_address, pattern="^add_new_address(_checkout)?$")],
            states={
                # Location sharing or manual entry choice
                ADDRESS_LOCATION: [
                    MessageHandler(filters.LOCATION, profile_handlers.location_received),
                    # Handle "Enter Manually" or "Re-enter Address" text buttons
                    MessageHandler(
                        filters.TEXT & filters.Regex(r"(?i).*(manual|enter manually|re-enter|✏️).*"),
                        profile_handlers.skip_location_sharing
                    ),
                    # Handle "Cancel" text button from retry keyboard
                    MessageHandler(
                        filters.TEXT & filters.Regex(r"(?i).*(cancel|❌ cancel).*"),
                        profile_handlers.cancel_address_text
                    ),
                ],
                # Address title input
                ADDRESS_TITLE: [
                    CallbackQueryHandler(profile_handlers.address_title_callback, pattern="^addr_title_"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.address_title_received)
                ],
                # Manual entry flow - Region selection
                ADDRESS_REGION: [
                    CallbackQueryHandler(profile_handlers.region_selected, pattern="^region_"),
                    CallbackQueryHandler(profile_handlers.cancel_address, pattern="^cancel_address_creation$"),
                ],
                # Manual entry flow - District selection
                ADDRESS_DISTRICT: [
                    CallbackQueryHandler(profile_handlers.district_selected, pattern="^district_"),
                    CallbackQueryHandler(profile_handlers.cancel_address, pattern="^cancel_address_creation$"),
                    CallbackQueryHandler(profile_handlers.back_to_region, pattern="^back_to_region$"),
                ],
                # Manual entry flow - Street input
                ADDRESS_STREET: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_street$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.street_received),
                ],
                # Manual entry flow - Building input
                ADDRESS_BUILDING: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_building$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.building_received),
                ],
                # Manual entry flow - Apartment input
                ADDRESS_APARTMENT: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_apartment$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.apartment_received),
                ],
                # Manual entry flow - Floor input
                ADDRESS_FLOOR: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_floor$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.floor_received),
                ],
                # Manual entry flow - Entrance input
                ADDRESS_ENTRANCE: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_entrance$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.entrance_received),
                ],
                # Delivery instructions input
                ADDRESS_DELIVERY_INSTRUCTIONS: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_delivery_instructions$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.delivery_instructions_received),
                ],
                # Geocode confirmation
                ADDRESS_GEOCODE_CONFIRM: [
                    CallbackQueryHandler(profile_handlers.confirm_geocode, pattern="^confirm_geocode$"),
                    CallbackQueryHandler(profile_handlers.retry_geocode, pattern="^retry_geocode$"),
                    CallbackQueryHandler(profile_handlers.cancel_address, pattern="^cancel_address_creation$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", profile_handlers.cancel_address),
                CallbackQueryHandler(profile_handlers.cancel_address, pattern="^cancel_address_creation$"),
            ],
            per_chat=True,
            per_user=True,
            name="address_conversation",
            conversation_timeout=600,  # 10 minutes timeout for manual entry
            allow_reentry=True
        )
        # Add conversation handler in higher priority group
        self.application.add_handler(address_handler, group=-2)
        logger.info(f"Address conversation handler registered with states: {list(address_handler.states.keys())}")

        # Phone verification conversation - for adding/updating phone number and name
        phone_verification_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(profile_handlers.add_phone_number, pattern="^add_phone_number$"),
            ],
            states={
                profile_handlers.PHONE_VERIFY_PHONE: [
                    MessageHandler(filters.CONTACT, profile_handlers.phone_verify_contact_received),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.phone_verify_text_received),
                ],
                profile_handlers.PHONE_VERIFY_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.phone_verify_name_received),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", profile_handlers.cancel_phone_verification),
                CallbackQueryHandler(profile_handlers.cancel_phone_verification, pattern="^cancel_phone_verification$"),
            ],
            per_chat=True,
            per_user=True,
            name="phone_verification",
            conversation_timeout=300,  # 5 minutes timeout
            allow_reentry=True
        )
        self.application.add_handler(phone_verification_handler, group=-2)
        logger.info(f"Phone verification conversation handler registered")

        # Subscription creation conversation
        subscription_creation_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.create_subscription_start, pattern="^create_subscription$")],
            states={
                subscription_handlers.SELECT_PRODUCTS: [
                    CallbackQueryHandler(subscription_handlers.select_products, pattern="^subscription_custom$"),
                    CallbackQueryHandler(subscription_handlers.select_products, pattern="^subscription_use_template$"),
                ],
                subscription_handlers.SELECT_QUANTITY: [
                    CallbackQueryHandler(subscription_handlers.select_quantity, pattern="^sub_product_"),
                    # Handle "add more items" button on product list
                    CallbackQueryHandler(subscription_handlers.add_more_items, pattern="^sub_add_more_items$"),
                    # Handle "done with items" button on product list
                    CallbackQueryHandler(subscription_handlers.items_selection_done, pattern="^sub_items_done$"),
                ],
                subscription_handlers.SELECT_FREQUENCY: [
                    # After selecting quantity, add item and show "add more" or "done" options
                    CallbackQueryHandler(subscription_handlers.add_item_with_quantity, pattern="^sub_qty_"),
                    # Handle "add more items" - go back to product selection
                    CallbackQueryHandler(subscription_handlers.add_more_items, pattern="^sub_add_more_items$"),
                    # Handle "done with items" - proceed to frequency selection
                    CallbackQueryHandler(subscription_handlers.items_selection_done, pattern="^sub_items_done$"),
                ],
                subscription_handlers.SELECT_ADDRESS: [
                    CallbackQueryHandler(subscription_handlers.select_address, pattern="^subscription_freq_"),
                ],
                subscription_handlers.SELECT_PAYMENT: [
                    CallbackQueryHandler(subscription_handlers.select_payment, pattern="^addr_"),
                ],
                subscription_handlers.CONFIRM_SUBSCRIPTION: [
                    CallbackQueryHandler(subscription_handlers.confirm_subscription, pattern="^sub_payment_"),
                    CallbackQueryHandler(subscription_handlers.create_subscription_confirmed, pattern="^confirm_create_subscription$"),
                    CallbackQueryHandler(subscription_handlers.cancel_subscription_creation, pattern="^cancel_subscription_creation$"),
                ],
            },
            fallbacks=[
                CallbackQueryHandler(subscription_handlers.cancel_subscription_creation, pattern="^cancel_subscription_creation$"),
                CommandHandler("cancel", subscription_handlers.cancel_subscription_creation)
            ],
            per_chat=True,
            per_user=True,
            name="subscription_creation",
            conversation_timeout=600,  # 10 minutes timeout
            allow_reentry=True
        )
        self.application.add_handler(subscription_creation_handler, group=-2)
        logger.info(f"Subscription creation conversation handler registered with states: {list(subscription_creation_handler.states.keys())}")

        # Subscription item management conversation
        item_management_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.add_item_start, pattern="^add_item_")],
            states={
                subscription_handlers.ITEM_SELECT_PRODUCT: [
                    CallbackQueryHandler(subscription_handlers.add_item_select_quantity, pattern="^sub_product_"),
                ],
                subscription_handlers.ITEM_SELECT_QUANTITY: [
                    CallbackQueryHandler(subscription_handlers.add_item_confirm, pattern="^sub_qty_"),
                    CallbackQueryHandler(subscription_handlers.update_item_confirm, pattern="^sub_qty_"),
                ],
            },
            fallbacks=[CommandHandler("cancel", subscription_handlers.cancel_subscription_creation)],
            per_chat=True,
            per_user=True,
            name="item_management",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(item_management_handler, group=-2)
        logger.info(f"Item management conversation handler registered")

        # Subscription frequency update conversation
        frequency_update_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.change_frequency, pattern="^change_frequency_")],
            states={
                0: [CallbackQueryHandler(subscription_handlers.update_frequency_confirm, pattern="^subscription_freq_")],
            },
            fallbacks=[],
            per_chat=True,
            per_user=True,
            name="frequency_update",
            conversation_timeout=300,
            allow_reentry=True,
            map_to_parent={}
        )
        # Note: This is handled inline, no need for separate conversation handler

        # Subscription payment method update conversation
        payment_update_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.change_payment_method_menu, pattern="^change_payment_")],
            states={
                0: [CallbackQueryHandler(subscription_handlers.change_payment_method_confirm, pattern="^sub_payment_")],
            },
            fallbacks=[],
            per_chat=True,
            per_user=True,
            name="payment_update",
            conversation_timeout=300,
            allow_reentry=True,
            map_to_parent={}
        )
        # Note: This is handled inline, no need for separate conversation handler

        # Item quantity update conversation
        update_item_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.update_item_quantity, pattern="^update_item_")],
            states={
                subscription_handlers.ITEM_SELECT_QUANTITY: [
                    CallbackQueryHandler(subscription_handlers.update_item_confirm, pattern="^sub_qty_"),
                ],
            },
            fallbacks=[],
            per_chat=True,
            per_user=True,
            name="update_item",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(update_item_handler, group=-2)
        logger.info(f"Update item conversation handler registered")

        # Message handlers (catch-all)
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)
        )

        # Contact messages are handled by ConversationHandlers in higher priority groups
        # (registration_handler and phone_verification_handler)

        # Location messages are handled by conversation handlers in higher priority groups

        # Handle voice messages if enabled
        if config.features.enable_voice_messages:
            self.application.add_handler(
                MessageHandler(filters.VOICE, self._handle_voice_message)
            )

    async def _setup_bot_commands(self):
        """Set up bot command menu"""
        command_key_pairs = [
            ("start", "telegram.bot.command.start_desc"),
            ("menu", "telegram.bot.command.menu_desc"),
            ("help", "telegram.bot.command.help_desc"),
            ("language", "telegram.bot.command.language_desc"),
        ]

        try:
            # Set localized commands per supported language.
            supported_languages = getattr(config.localization, 'supported_languages', []) or ['en']
            for language in supported_languages:
                localized_commands = [
                    BotCommand(command, i18n.get(key, language))
                    for command, key in command_key_pairs
                ]
                await self.application.bot.set_my_commands(
                    localized_commands,
                    language_code=language
                )

            # Set default commands (fallback to English if locale is unknown).
            default_commands = [
                BotCommand(command, i18n.get(key, 'en'))
                for command, key in command_key_pairs
            ]
            await self.application.bot.set_my_commands(default_commands)
            logger.info("Bot commands set successfully")

        except Exception as e:
            logger.error(f"Failed to set bot commands: {e}")

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages not caught by other handlers"""
        try:
            user_id = update.effective_user.id
            text = update.message.text.strip()

            logger.info(f"=== GENERAL TEXT MESSAGE HANDLER CALLED ===")
            logger.info(f"User: {user_id}, Message: {text}")

            # Apply rate limiting
            if not await rate_limiter.allow_request(user_id):
                language = await i18n.get_user_language(user_id)
                await update.message.reply_text(
                    i18n.get('telegram.bot.rate_limit_exceeded', language)
                )
                return

            # Apply user middleware
            user = await user_middleware(update)
            if not user:
                return

            language = await i18n.get_user_language(user_id)

            # Check if user is awaiting OTP verification (stored in context)
            if context.user_data.get('awaiting_otp'):
                prompted_update_id = context.user_data.get('otp_prompted_update_id')
                if prompted_update_id == update.update_id:
                    # OTP was just prompted from this same update (e.g. phone text).
                    # Skip immediate re-processing of the same text as OTP.
                    context.user_data.pop('otp_prompted_update_id', None)
                    return
                await self._handle_otp_verification(update, context, text, language)
                return

            # Check if user is in a conversation state
            user_state = await self.user_repository.get_user_state(user_id)

            if user_state.get('awaiting_input'):
                # Handle contextual input
                await self._handle_contextual_input(update, context, user_state, language)
            else:
                # General free text with no active flow (not OTP, not a conversation
                # state): silently capture it as a support message so an admin can
                # reply from the admin UI. No auto-acknowledgement is sent.
                await self._capture_support_message(update, context, text)

        except Exception as e:
            logger.error(f"Error handling text message: {e}")
            await update.message.reply_text(i18n.get('telegram.error_occurred', language))

    async def _capture_support_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       text: str):
        """Silently persist an unsolicited free-text message so an admin can reply
        from the admin UI. No auto-acknowledgement is sent to the customer."""
        try:
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    await client.record_support_message(user_token, text)
                else:
                    logger.warning(
                        "Support capture skipped: no auth token for user %s",
                        update.effective_user.id,
                    )
        except Exception as exc:
            logger.error(f"Failed to record support message: {exc}")

    async def _handle_otp_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       text: str, language: str):
        """Handle OTP verification from user input"""
        try:
            user_id = update.effective_user.id

            # Validate OTP format (6 digits)
            if not text.isdigit() or len(text) != 6:
                await update.message.reply_text(
                    i18n.get('telegram.bot.otp.invalid_format', language)
                )
                return

            # Verify OTP via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await update.message.reply_text(
                        i18n.get('telegram.bot.otp.auth_error', language)
                    )
                    context.user_data.pop('awaiting_otp', None)
                    context.user_data.pop('pending_phone_verification', None)
                    context.user_data.pop('otp_prompted_update_id', None)
                    return

                response = await client.verify_phone_otp(user_token, text)
                if response.success:
                    await update.message.reply_text(
                        i18n.get('telegram.bot.otp.success_message', language),
                        parse_mode='Markdown',
                        reply_markup=await main_menu_for(update.effective_user.id, language)
                    )

                    # Clear OTP flags
                    context.user_data.pop('awaiting_otp', None)
                    context.user_data.pop('pending_phone_verification', None)
                    context.user_data.pop('otp_prompted_update_id', None)

                    logger.info(f"Phone verification successful for user {user_id}")
                else:
                    await update.message.reply_text(
                        i18n.get(
                            'telegram.bot.otp.failed_with_reason',
                            language,
                            error=response.error
                        )
                    )

        except Exception as e:
            logger.error(f"Error verifying OTP: {e}")
            await update.message.reply_text(
                i18n.get('telegram.bot.otp.failed_generic', language)
            )
            context.user_data.pop('awaiting_otp', None)
            context.user_data.pop('pending_phone_verification', None)
            context.user_data.pop('otp_prompted_update_id', None)

    async def _handle_contextual_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     user_state: Dict, language: str):
        """Handle input based on user's current state"""
        input_type = user_state.get('awaiting_input')
        user_id = update.effective_user.id
        text = update.message.text.strip()

        if input_type == 'search_products':
            # Handle product search
            await product_handlers.search_products(update, context, text)
        elif input_type == 'support_message':
            # Handle support message
            await support_handlers.handle_support_message(update, context, text)
        elif input_type == 'edit_address_title':
            # Handle address title editing
            await profile_handlers.handle_address_title_edit(update, context, text, user_state)
        elif input_type == 'edit_address_instructions':
            # Handle address instructions editing
            await profile_handlers.handle_address_instructions_edit(update, context, text, user_state)
        elif input_type == 'edit_profile_name':
            # Handle profile name editing
            await profile_handlers.handle_profile_name_edit(update, context, text, user_state)
        elif input_type == 'edit_profile_birthday':
            # Handle profile birthday editing (DD-MM-YYYY text entry)
            await profile_handlers.handle_profile_birthday_edit(update, context, text, user_state)
        else:
            # Unknown state, clear it
            await self.user_repository.update_user_state(user_id, {})
            await update.message.reply_text(i18n.get('telegram.error.invalid_input', language))

    async def _handle_general_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  text: str, language: str):
        """Handle general text input"""
        # Check for common keywords
        text_lower = text.lower()

        if any(keyword in text_lower for keyword in ['help', 'support', 'problem', 'issue']):
            await support_handlers.support_menu(update, context)
        elif any(keyword in text_lower for keyword in ['order', 'buy', 'purchase', 'water']):
            await product_handlers.products_menu(update, context)
        elif any(keyword in text_lower for keyword in ['track', 'delivery', 'status']):
            await order_handlers.orders_menu(update, context)
        else:
            # Default response
            logger.info(f"General text input from user {update.effective_user.id}: {text}")
            await main_menu_handler(update, context)

    async def _handle_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle contact sharing"""
        try:
            user_id = update.effective_user.id
            contact = update.message.contact

            if contact.user_id == user_id:
                # User shared their own contact
                phone = contact.phone_number
                await self.user_repository.set_user_phone(user_id, phone)

                language = await i18n.get_user_language(user_id)
                await update.message.reply_text(
                    i18n.get('telegram.registration.phone_shared', language),
                    reply_markup=None
                )

                # Continue with registration if needed
                await profile_handlers.continue_registration(update, context)

        except Exception as e:
            logger.error(f"Error handling contact: {e}")

    async def _handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle location sharing outside of conversation"""
        try:
            logger.info(f"=== GENERAL LOCATION HANDLER CALLED ===")
            logger.info(f"User: {update.effective_user.id}")
            location = update.message.location
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            logger.info(f"Location: lat={location.latitude}, lng={location.longitude}")
            logger.info(f"Context user_data: {context.user_data}")

            # Check if user is in address adding flow
            user_state = await self.user_repository.get_user_state(user_id)
            logger.info(f"Database user state: {user_state}")

            if user_state.get('awaiting_input') == 'address_location':
                logger.info(f"User is in address_location flow, handling as address creation")
                # Handle as part of address creation
                user_state['temp_location'] = {
                    'latitude': location.latitude,
                    'longitude': location.longitude
                }
                await self.user_repository.update_user_state(user_id, user_state)

                await update.message.reply_text(
                    i18n.get('telegram.bot.location.received_prompt', language),
                    reply_markup=ReplyKeyboardRemove()
                )

                # Set state for address title input
                user_state['awaiting_input'] = 'address_title'
                await self.user_repository.update_user_state(user_id, user_state)
                logger.info(f"Updated user state to address_title")
            else:
                logger.info(f"Location shared outside of any specific flow, showing general response")
                # Location shared outside of any specific flow
                await update.message.reply_text(
                    i18n.get(
                        'telegram.bot.location.shared_general',
                        language,
                        latitude=location.latitude,
                        longitude=location.longitude
                    ),
                    reply_markup=await main_menu_for(update.effective_user.id, language)
                )

        except Exception as e:
            logger.error(f"Error handling location: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    async def _handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice messages (if voice feature is enabled)"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # For now, just acknowledge voice message
            # In the future, could integrate speech-to-text
            await update.message.reply_text(
                i18n.get('telegram.bot.voice.not_supported', language),
            )

        except Exception as e:
            logger.error(f"Error handling voice message: {e}")

    def run(self):
        """Run the bot (synchronous wrapper for async run)"""
        try:
            # Use synchronous run methods that handle their own event loops
            if config.telegram.webhook_url:
                # Run with webhook
                logger.info(f"Starting bot with webhook: {config.telegram.webhook_url}")
                self.application.run_webhook(
                    listen=config.telegram.webhook_listen,
                    port=config.telegram.webhook_port,
                    webhook_url=config.telegram.webhook_url,
                    cert=config.telegram.webhook_ssl_cert,
                    key=config.telegram.webhook_ssl_priv,
                )
            else:
                # Run with polling
                logger.info("========================================")
                logger.info("STARTING BOT WITH POLLING MODE")
                logger.info("========================================")
                logger.info("Bot will start receiving updates from Telegram...")
                self.is_running = True

                # Start polling with verbose logging
                self.application.run_polling(
                    timeout=config.telegram.polling_timeout,
                    poll_interval=config.telegram.poll_interval,
                    bootstrap_retries=config.telegram.bootstrap_retries,
                    allowed_updates=None,  # Accept all update types
                    drop_pending_updates=config.telegram.drop_pending_updates
                )

        except Exception as e:
            logger.error(f"Error running bot: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def async_initialize(self):
        """Async initialization wrapper"""
        try:
            await self.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")
            raise

    async def cleanup(self):
        """Cleanup resources"""
        try:
            logger.info("Cleaning up bot resources...")

            # Stop webhook server
            logger.info("Stopping webhook server...")
            await webhook_server.stop()

            # Close TokenManager Redis connection
            if self.token_manager:
                logger.info("Closing TokenManager...")
                await self.token_manager.close()

            # Close database connection
            if db_manager.is_connected:
                await db_manager.disconnect()

            self.is_running = False
            logger.info("Bot cleanup completed")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    async def stop(self):
        """Stop the bot gracefully"""
        logger.info("Stopping bot...")

        if self.application:
            await self.application.stop()

        await self.cleanup()


# Global bot instance
bot = WaterBusinessBot()


def main():
    """Main entry point"""
    # Set up logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO if config.telegram.webhook_url else logging.DEBUG
    )

    # Set up signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}")
        if bot.application and hasattr(bot.application, 'stop'):
            try:
                asyncio.create_task(bot.stop())
            except RuntimeError:
                # No running event loop, just exit
                sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Initialize the bot first (async)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.async_initialize())

        # Don't close the loop - let run_polling use it
        # Then run the bot (sync - it uses the current event loop)
        bot.run()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Use uvloop for better performance on Unix systems
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    main()
