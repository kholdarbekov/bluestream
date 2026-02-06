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

# Initialize Sentry if DSN is configured
_sentry_dsn = os.environ.get('SENTRY_DSN')
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get('SENTRY_ENVIRONMENT', os.environ.get('FLASK_ENV', 'development')),
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '1.0')),
        profiles_sample_rate=float(os.environ.get('SENTRY_PROFILES_SAMPLE_RATE', '1.0')),
        send_default_pii=os.environ.get('SENTRY_SEND_DEFAULT_PII', 'false').lower() == 'true',
        debug=os.environ.get('SENTRY_DEBUG', 'false').lower() == 'true',
        integrations=[
            AsyncioIntegration(),
        ],
        # Set release version if available
        release=os.environ.get('APP_VERSION', 'telegram-bot@1.0.0'),
    )

from telegram import Update, BotCommand, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, TypeHandler,
    PreCheckoutQueryHandler
)
from telegram.error import TelegramError

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
    start_handler, main_menu_handler, language_handler,
    product_handlers, order_handlers, subscription_handlers,
    profile_handlers, loyalty_handlers, admin_handlers,
    support_handlers, payment_handlers
)
# Import conversation states directly (they are module-level constants)
from handlers.profile import (
    SELECT_LANGUAGE, PHONE, LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP,
    ADDRESS_LOCATION, ADDRESS_TITLE, ADDRESS_REGION, ADDRESS_DISTRICT,
    ADDRESS_STREET, ADDRESS_BUILDING, ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_ENTRANCE, ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM
)
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
            self.application = (
                Application.builder()
                .token(config.telegram.bot_token)
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

            # Add middleware to log ALL updates
            async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
                logger.info(f"!!! UPDATE RECEIVED: type={type(update).__name__}")
                if update.message:
                    # Check for successful payment specifically
                    if update.message.successful_payment:
                        logger.info("=" * 70)
                        logger.info("!!! SUCCESSFUL_PAYMENT MESSAGE RECEIVED !!!")
                        logger.info(f"User: {update.effective_user.id}")
                        logger.info(f"Payment: {update.message.successful_payment}")
                        logger.info(f"Amount: {update.message.successful_payment.total_amount} {update.message.successful_payment.currency}")
                        logger.info(f"Telegram charge ID: {update.message.successful_payment.telegram_payment_charge_id}")
                        logger.info(f"Provider charge ID: {update.message.successful_payment.provider_payment_charge_id}")
                        logger.info(f"Payload: {update.message.successful_payment.invoice_payload}")
                        logger.info("=" * 70)
                    else:
                        logger.info(f"!!! Message update from user {update.effective_user.id}, text: {update.message.text[:100] if update.message.text else 'NO TEXT'}")
                if update.callback_query:
                    logger.info(f"!!! CALLBACK QUERY: {update.callback_query.data} from user {update.effective_user.id}")
                if update.edited_message:
                    logger.info(f"!!! Edited message update")
                if update.pre_checkout_query:
                    logger.info("=" * 70)
                    logger.info("!!! PRE_CHECKOUT_QUERY RECEIVED !!!")
                    logger.info(f"User: {update.effective_user.id}")
                    logger.info(f"Query ID: {update.pre_checkout_query.id}")
                    logger.info(f"From: {update.pre_checkout_query.from_user}")
                    logger.info(f"Amount: {update.pre_checkout_query.total_amount} {update.pre_checkout_query.currency}")
                    logger.info(f"Payload: {update.pre_checkout_query.invoice_payload}")
                    logger.info(f"Shipping option: {update.pre_checkout_query.shipping_option_id}")
                    logger.info(f"Order info: {update.pre_checkout_query.order_info}")
                    logger.info("=" * 70)

            
            self.application.add_handler(TypeHandler(Update, log_all_updates), group=-10)
            logger.info("Update logging middleware installed!")

            logger.info("Bot initialization completed successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
    
    async def _setup_handlers(self):
        """Set up all bot handlers"""
        
        # Command handlers
        # self.application.add_handler(CommandHandler("start", start_handler))
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
            
            # Order callbacks
            CallbackQueryHandler(order_handlers.orders_menu, pattern="^menu_orders$"),
            CallbackQueryHandler(order_handlers.order_details, pattern="^order_"),
            CallbackQueryHandler(order_handlers.checkout_handler, pattern="^checkout"),
            CallbackQueryHandler(order_handlers.address_handler, pattern="^address_"),
            CallbackQueryHandler(order_handlers.payment_handler, pattern="^payment_(cash|card|payme|click|uzcard|humo|loyalty_points|business_account)$"),
            CallbackQueryHandler(order_handlers.confirm_order, pattern="^confirm_order"),
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
            # add_phone_number is now handled by phone_verification_handler ConversationHandler
            CallbackQueryHandler(profile_handlers.verify_phone_number, pattern="^verify_phone_number$"),
            CallbackQueryHandler(profile_handlers.edit_profile, pattern="^edit_profile$"),
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
            
            # Loyalty callbacks
            CallbackQueryHandler(loyalty_handlers.loyalty_menu, pattern="^menu_loyalty$"),
            CallbackQueryHandler(loyalty_handlers.loyalty_history, pattern="^loyalty_history$"),
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

        # Telegram Payments handlers (Pre-checkout and Successful Payment)
        # PreCheckoutQuery - CRITICAL: Must respond within 10 seconds
        self.application.add_handler(
            PreCheckoutQueryHandler(payment_handlers.handle_pre_checkout_query)
        )

        # Successful Payment message handler
        self.application.add_handler(
            MessageHandler(
                filters.SUCCESSFUL_PAYMENT,
                payment_handlers.handle_successful_payment
            )
        )

        # Add logging callback handler to catch all callbacks for debugging
        async def debug_callback_handler(update, context):
            if update.callback_query:
                logger.error(f"========================================")
                logger.error(f"=== CALLBACK QUERY RECEIVED ===")
                logger.error(f"User: {update.effective_user.id}")
                logger.error(f"Callback data: {update.callback_query.data}")
                logger.error(f"Message ID: {update.callback_query.message.message_id}")
                logger.error(f"========================================")
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
                    MessageHandler(filters.TEXT, profile_handlers.phone_text_received)
                ],
                # Account linking states
                LINK_ACCOUNT_CONFIRM: [
                    CallbackQueryHandler(profile_handlers.link_account_confirm, pattern="^link_")
                ],
                LINK_ACCOUNT_OTP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.link_account_otp)
                ],
                # profile_handlers.NAME: [
                #     MessageHandler(filters.TEXT & ~filters.COMMAND, profile_handlers.name_received)
                # ],
            },
            fallbacks=[CommandHandler("cancel", profile_handlers.cancel_registration)]
        )
        self.application.add_handler(registration_handler, group=-2)
        
        # Address input conversation - Enhanced flow with manual entry support
        address_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(profile_handlers.add_address, pattern="^add_new_address$")],
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
        commands = [
            BotCommand("start", "Start the bot and show main menu"),
            BotCommand("menu", "Show main menu"),
            BotCommand("help", "Get help and support"),
            BotCommand("language", "Change language settings"),
        ]
        
        try:
            # Set default commands for all users
            await self.application.bot.set_my_commands(commands)
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
                await update.message.reply_text("⏳ Please slow down. Try again in a moment.")
                return

            # Apply user middleware
            user = await user_middleware(update)
            if not user:
                return

            language = await i18n.get_user_language(user_id)

            # Check if user is awaiting OTP verification (stored in context)
            if context.user_data.get('awaiting_otp'):
                await self._handle_otp_verification(update, context, text, language)
                return

            # Check if user is in a conversation state
            user_state = await self.user_repository.get_user_state(user_id)

            if user_state.get('awaiting_input'):
                # Handle contextual input
                await self._handle_contextual_input(update, context, user_state, language)
            # else:
            #     # Handle general text input (could be search, commands, etc.)
            #     logger.info(f"Handling general text input from user {user_id}: {text}")
            #     await self._handle_general_input(update, context, text, language)
                
        except Exception as e:
            logger.error(f"Error handling text message: {e}")
            await update.message.reply_text(i18n.get('telegram.error_occurred', language))

    async def _handle_otp_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       text: str, language: str):
        """Handle OTP verification from user input"""
        try:
            user_id = update.effective_user.id

            # Validate OTP format (6 digits)
            if not text.isdigit() or len(text) != 6:
                await update.message.reply_text(
                    "❌ Invalid code format. Please enter the 6-digit verification code:"
                )
                return

            # Verify OTP via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await update.message.reply_text(
                        "❌ Authentication error. Please try again later."
                    )
                    context.user_data.pop('awaiting_otp', None)
                    context.user_data.pop('pending_phone_verification', None)
                    return

                response = await client.verify_phone_otp(user_token, text)
                if response.success:
                    await update.message.reply_text(
                        "✅ *Phone verified successfully!*\n\n"
                        "Your phone number has been verified. "
                        "You can now place orders and receive notifications.",
                        parse_mode='Markdown',
                        reply_markup=MenuKeyboards.main_menu(language)
                    )

                    # Clear OTP flags
                    context.user_data.pop('awaiting_otp', None)
                    context.user_data.pop('pending_phone_verification', None)

                    logger.info(f"Phone verification successful for user {user_id}")
                else:
                    await update.message.reply_text(
                        f"❌ Verification failed: {response.error}\n\n"
                        "Please enter the correct code or click /cancel to stop:"
                    )

        except Exception as e:
            logger.error(f"Error verifying OTP: {e}")
            await update.message.reply_text(
                "❌ Verification failed. Please try again later."
            )
            context.user_data.pop('awaiting_otp', None)
            context.user_data.pop('pending_phone_verification', None)

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
                    "📍 Location received! Please provide a title for this address:",
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
                    f"📍 Thanks for sharing your location!\n\n"
                    f"Lat: {location.latitude}, Lng: {location.longitude}\n\n"
                    f"If you want to add this as a delivery address, please go to:\n"
                    f"Profile → Addresses → Add Address",
                    reply_markup=MenuKeyboards.main_menu(language)
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
                "🎙️ Voice message received! Currently, I can only respond to text messages. "
                "Please type your message or use the menu buttons.",
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
                    timeout=10,  # Wait up to 10 seconds for updates
                    bootstrap_retries=3,  # Retry bootstrap up to 3 times
                    allowed_updates=None,  # Accept all update types
                    drop_pending_updates=True  # Clear any pending updates
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