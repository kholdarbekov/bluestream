"""
Main Staff Bot Application
Staff bot for delivery persons and operators
"""
import asyncio
import logging
import signal
import sys
import os
import re
from typing import Optional

# Initialize Sentry before any other imports
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration

_sentry_dsn = os.environ.get('SENTRY_DSN')
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get('SENTRY_ENVIRONMENT', os.environ.get('FLASK_ENV', 'development')),
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '1.0')),
        profiles_sample_rate=float(os.environ.get('SENTRY_PROFILES_SAMPLE_RATE', '1.0')),
        send_default_pii=os.environ.get('SENTRY_SEND_DEFAULT_PII', 'false').lower() == 'true',
        debug=os.environ.get('SENTRY_DEBUG', 'false').lower() == 'true',
        integrations=[AsyncioIntegration()],
        release=os.environ.get('APP_VERSION', 'staff-bot@1.0.0'),
    )

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, TypeHandler
)
from telegram.error import NetworkError, TimedOut
from shared.telegram_request import ResilientHTTPXRequest

# Setup logging
from logging_config import setup_logging, log_bot_startup_info

log_level = os.getenv('LOG_LEVEL', 'INFO')
setup_logging(log_level=log_level, log_to_file=False)

# Bot modules
from staff_bot.config import config
from staff_bot.database import db_manager, StaffUserRepository
from staff_bot.i18n import i18n
from staff_bot.api_client import api_client
from webhook_server import webhook_server
from staff_bot.token_manager import TokenManager
from staff_bot.handlers.start import StartHandler, SELECT_LANGUAGE
from staff_bot.handlers.menu import main_menu_handler, menu_handler
from staff_bot.handlers.language import LanguageHandler
from staff_bot.handlers.tryouts import TryoutHandler
from staff_bot.handlers.tryouts import ENTER_TRYOUT_PHONE, ENTER_TRYOUT_NAME, ENTER_TRYOUT_ADDRESS
from staff_bot.handlers.delivery import (
    OrdersPoolHandler, ActiveDeliveryHandler, StatusUpdateHandler, CashCollectionHandler,
    BottleCollectionHandler, HistoryHandler, LocationHandler
)
from staff_bot.handlers.delivery.status_update import BOTTLE_RETURN_INPUT
from staff_bot.handlers.delivery.bottle_collection import (
    BOTTLE_COLLECTION_SEARCH_INPUT, BOTTLE_COLLECTION_QTY_INPUT,
    BOTTLE_COLLECTION_NOTE_INPUT, BOTTLE_FINE_QTY_INPUT,
    BOTTLE_FINE_AMOUNT_INPUT, BOTTLE_FINE_NOTE_INPUT,
    BOTTLES_LOADED_INPUT, BOTTLES_RETURNED_WH_INPUT,
    BOTTLE_SESSION_LOADED_QTY_INPUT, BOTTLE_SESSION_RETURNED_QTY_INPUT,
    BOTTLE_TRANSFER_DRIVER_SELECT, BOTTLE_TRANSFER_QTY_INPUT,
    BOTTLE_TRANSFER_CONFIRM_QTY_INPUT,
)
from staff_bot.handlers.delivery.bottle_session import BottleSessionMembershipHandler
from staff_bot.handlers.operator.create_user import CreateUserHandler
from staff_bot.handlers.operator.create_user import ENTER_PHONE, ENTER_FIRST_NAME, ENTER_LAST_NAME
from staff_bot.handlers.operator.create_user import SELECT_LANGUAGE as CREATE_USER_LANG
from staff_bot.handlers.operator.create_user import CONFIRM_CREATE
from staff_bot.handlers.operator.search_user import SearchUserHandler, SEARCH_INPUT
from staff_bot.handlers.operator.create_order import (
    CreateOrderHandler,
    SELECT_CLIENT as ORDER_SELECT_CLIENT,
    SELECT_ADDRESS as ORDER_SELECT_ADDRESS,
    SELECT_PRODUCTS as ORDER_SELECT_PRODUCTS,
    SELECT_QUANTITY as ORDER_SELECT_QUANTITY,
    SELECT_PAYMENT as ORDER_SELECT_PAYMENT,
    ENTER_NOTES as ORDER_ENTER_NOTES,
    CONFIRM_ORDER as ORDER_CONFIRM_ORDER,
)
from staff_bot.handlers.operator.manage_address import (
    ManageAddressHandler,
    ENTER_LABEL, ENTER_ADDRESS, ENTER_DISTRICT,
    ENTER_NOTES as ADDR_ENTER_NOTES, CONFIRM_ADDRESS
)
from staff_bot.handlers.operator.recent_orders import RecentOrdersHandler
from staff_bot.handlers.operator.orders_pool_view import OperatorOrdersPoolViewHandler
from staff_bot.handlers.common.profile import ProfileHandler
from staff_bot.handlers.common.help import HelpHandler

logger = logging.getLogger('staff_bot')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler"""
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning(
            "Transient Telegram network error while handling update: %s",
            context.error,
        )
        return

    logger.error(f"Error handling update: {context.error}", exc_info=context.error)

    if isinstance(update, Update) and update.effective_user:
        try:
            language = i18n.normalize_language(context.user_data.get('language'))
            error_msg = i18n.get('staff.error_occurred', language)
            if update.callback_query:
                await update.callback_query.answer(error_msg, show_alert=True)
            elif update.message:
                await update.message.reply_text(error_msg)
        except Exception:
            pass


class StaffBot:
    """Main staff bot application class"""

    def __init__(self):
        self.application: Optional[Application] = None
        self.is_running = False
        self.user_repository = StaffUserRepository(db_manager)
        self.token_manager: Optional[TokenManager] = None

    async def initialize(self):
        """Initialize bot and all dependencies"""
        try:
            log_bot_startup_info()
            logger.info("Initializing Staff Bot...")

            # Initialize database connection
            await db_manager.connect()
            await self._validate_database_schema()

            # Initialize the persistent backend HTTP client. Owned by the bot
            # lifecycle so all handlers reuse a single httpx.AsyncClient with a
            # shared connection pool — avoids per-request TLS handshakes and
            # the concurrent-close race that the previous per-call build had.
            await api_client.start()

            # Load translations
            await i18n.load_translations()

            # Initialize TokenManager for JWT token caching
            self.token_manager = TokenManager(config.redis.url)
            if await self.token_manager.connect():
                logger.info("TokenManager connected to Redis successfully")
            else:
                logger.warning("TokenManager running without Redis - tokens will not be cached")

            # C-2: install the flow-state mirror's Redis client. We share the
            # TokenManager's connection — same Redis instance, same lifecycle.
            # If TokenManager couldn't connect we pass None and flow_state
            # silently degrades to "no flow active" for every check, which
            # matches the pre-flow-marker behaviour exactly.
            from staff_bot.utils import flow_state
            flow_state.configure(self.token_manager.redis if self.token_manager._connected else None)

            # Build Telegram application
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

            # Start webhook server for staff notifications
            webhook_server.set_application(self.application)
            await webhook_server.start()

            # Test bot connection
            bot_info = await self.application.bot.get_me()
            logger.info(f"Staff Bot connected: @{bot_info.username} ({bot_info.first_name})")

            # Store token_manager in bot_data for handler access
            self.application.bot_data['token_manager'] = self.token_manager

            # Set up handlers
            await self._setup_handlers()

            # Set up bot commands
            await self._setup_bot_commands()

            # Set up error handling
            self.application.add_error_handler(error_handler)

            # Add update logging middleware
            async def log_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
                user_id = update.effective_user.id if update.effective_user else 'N/A'
                if update.callback_query:
                    logger.info(f"Update: callback_query={update.callback_query.data}, user={user_id}")
                elif update.message:
                    logger.info(f"Update: message, user={user_id}")

            self.application.add_handler(TypeHandler(Update, log_updates), group=-10)

            logger.info("Staff Bot initialization completed successfully")

        except Exception as e:
            logger.error(f"Failed to initialize staff bot: {e}", exc_info=True)
            raise

    async def _validate_database_schema(self):
        """Validate required DB schema for staff bot startup."""
        skip_schema_check = os.environ.get(
            'STAFF_SKIP_SCHEMA_CHECK', 'false'
        ).lower() == 'true'
        if skip_schema_check:
            logger.warning("Skipping staff schema validation due to STAFF_SKIP_SCHEMA_CHECK=true")
            return

        required_user_columns = {'staff_roles', 'staff_bot_state'}
        rows = await db_manager.fetchall(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'users'
              AND column_name = ANY($1::text[])
            """,
            list(required_user_columns),
        )
        existing_columns = {row['column_name'] for row in rows}
        missing_columns = sorted(required_user_columns - existing_columns)
        if missing_columns:
            raise RuntimeError(
                "Missing required users table columns for staff bot: "
                f"{', '.join(missing_columns)}. "
                "Apply database migrations before starting staff bot."
            )

        staff_activity_table = await db_manager.fetchval(
            "SELECT to_regclass('public.staff_activity_log')"
        )
        if not staff_activity_table:
            raise RuntimeError(
                "Missing required table `staff_activity_log`. "
                "Apply database migrations before starting staff bot."
            )

    async def _route_new_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Route the unified `New Orders` entry to the correct pool view based on staff role.
        Dual-role users default to the delivery driver (actionable) view.
        """
        roles = context.user_data.get('staff_roles', []) or []
        if 'delivery_driver' in roles:
            await self._delivery_handlers['orders_pool'].show_pool(update, context)
        elif 'operator' in roles:
            await self._operator_handlers['orders_pool_view'].show_pool(update, context)

    @staticmethod
    def _menu_text_pattern(translation_key: str) -> str:
        """Regex pattern to match reply-keyboard label with or without emoji prefix."""
        labels = []
        for lang_code in i18n.supported_languages:
            label = i18n.get(translation_key, lang_code).strip()
            if label:
                labels.append(re.escape(label))

        unique_labels = sorted(set(labels), key=len, reverse=True)
        if not unique_labels:
            # Never match if label list is empty.
            return r"$a"

        alternatives = "|".join(unique_labels)
        return r"^\s*(?:\S+\s+)?(?:%s)\s*$" % alternatives

    async def _setup_handlers(self):
        """Set up all bot handlers"""
        start_handler = StartHandler()
        language_handler = LanguageHandler()
        self._language_handler = language_handler

        # Delivery handlers
        orders_pool_handler = OrdersPoolHandler()
        active_delivery_handler = ActiveDeliveryHandler()
        status_update_handler = StatusUpdateHandler()
        cash_collection_handler = CashCollectionHandler()
        bottle_collection_handler = BottleCollectionHandler()
        bottle_session_membership_handler = BottleSessionMembershipHandler()
        history_handler = HistoryHandler()
        location_handler = LocationHandler()
        tryout_handler = TryoutHandler()

        # Operator handlers
        create_user_handler = CreateUserHandler()
        search_user_handler = SearchUserHandler()
        create_order_handler = CreateOrderHandler()
        manage_address_handler = ManageAddressHandler()
        recent_orders_handler = RecentOrdersHandler()
        operator_orders_pool_view_handler = OperatorOrdersPoolViewHandler()

        # Common handlers
        profile_handler = ProfileHandler()
        help_handler_instance = HelpHandler()

        # ------------------------------------------------------------------
        # Conversation-fallback wrappers
        # ------------------------------------------------------------------
        # PTB ConversationHandler keeps the user in their current state when a
        # fallback handler returns None.  `status_update_handler.show_cash_hub`
        # and `main_menu_handler` are reused as both regular menu handlers
        # *and* conversation fallbacks — they correctly re-render the menu but
        # don't return ConversationHandler.END, so the conversation never
        # terminates and the user's next text input is captured by the
        # in-state MessageHandler (e.g. parsed as a bottle quantity).
        #
        # That is exactly the trap a driver hit when, after BOTTLE_SESSION_REQUIRED
        # → "Open session" → quantity prompt, they tapped the inline Back button
        # and the bot kept asking for the quantity.  These tiny wrappers delegate
        # to the underlying handler, then explicitly close the conversation.
        async def _exit_to_cash_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await status_update_handler.show_cash_hub(update, context)
            return ConversationHandler.END

        async def _exit_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await main_menu_handler(update, context)
            return ConversationHandler.END

        # Store handler instances for text message routing
        self._delivery_handlers = {
            'orders_pool': orders_pool_handler,
            'active_delivery': active_delivery_handler,
            'status_update': status_update_handler,
            'cash_collection': cash_collection_handler,
            'bottle_collection': bottle_collection_handler,
            'history': history_handler,
            'location': location_handler,
            'tryouts': tryout_handler,
        }
        self._operator_handlers = {
            'orders_pool_view': operator_orders_pool_view_handler,
            'create_user': create_user_handler,
            'search_user': search_user_handler,
            'create_order': create_order_handler,
            'manage_address': manage_address_handler,
            'recent_orders': recent_orders_handler,
        }
        self._common_handlers = {
            'profile': profile_handler,
            'help': help_handler_instance,
        }

        # Registration/Authentication conversation handler
        auth_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start_handler.start)],
            states={
                SELECT_LANGUAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, start_handler.language_selected)
                ],
            },
            fallbacks=[CommandHandler("cancel", start_handler.cancel)],
            per_chat=True,
            per_user=True,
            name="staff_auth",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(auth_handler, group=-2)

        # Command handlers
        self.application.add_handler(CommandHandler("menu", menu_handler))
        self.application.add_handler(CommandHandler("help", self._help_handler))
        self.application.add_handler(CommandHandler("language", language_handler.language_menu))

        # Callback query handlers
        callback_handlers = [
            # Language / Settings
            CallbackQueryHandler(language_handler.language_menu, pattern="^staff_settings$"),
            CallbackQueryHandler(language_handler.set_language, pattern="^staff_set_language_"),

            # Unified entry points
            CallbackQueryHandler(self._route_new_orders, pattern="^staff_new_orders_unified$"),
            CallbackQueryHandler(tryout_handler.show_hub, pattern="^staff_tryouts_hub$"),
            CallbackQueryHandler(status_update_handler.show_cash_hub, pattern="^staff_cash_hub$"),

            # --- Delivery handlers ---
            # Order pool
            CallbackQueryHandler(orders_pool_handler.show_pool, pattern="^staff_new_orders$"),
            CallbackQueryHandler(orders_pool_handler.view_order_details, pattern=r"^staff_view_order_\d+$"),
            CallbackQueryHandler(orders_pool_handler.accept_order, pattern=r"^staff_accept_order_\d+$"),
            CallbackQueryHandler(orders_pool_handler.confirm_accept, pattern=r"^staff_confirm_accept_\d+$"),
            CallbackQueryHandler(orders_pool_handler.pool_pagination, pattern=r"^staff_pool_page_\d+$"),

            # Active deliveries
            CallbackQueryHandler(active_delivery_handler.show_active_deliveries, pattern="^staff_active_deliveries$"),
            CallbackQueryHandler(active_delivery_handler.view_active_delivery, pattern=r"^staff_view_active_\d+$"),
            CallbackQueryHandler(active_delivery_handler.navigate_to_address, pattern=r"^staff_navigate_\d+$"),
            CallbackQueryHandler(active_delivery_handler.optimize_routes, pattern="^staff_optimize_routes$"),
            CallbackQueryHandler(active_delivery_handler.share_location_prompt, pattern="^staff_share_location_prompt$"),
            CallbackQueryHandler(active_delivery_handler.decline_suggestion, pattern=r"^staff_decline_suggestion_\d+$"),

            # Try-outs
            CallbackQueryHandler(tryout_handler.show_create_products, pattern="^staff_tryout_select_products$"),
            CallbackQueryHandler(tryout_handler.show_task_pool, pattern="^staff_tryout_tasks$"),
            CallbackQueryHandler(tryout_handler.show_active_tryouts, pattern="^staff_tryout_active$"),
            CallbackQueryHandler(tryout_handler.select_create_product, pattern=r"^staff_tryout_product_\d+$"),
            CallbackQueryHandler(tryout_handler.select_create_quantity, pattern=r"^staff_tryout_qty_\d+_\d+$"),
            CallbackQueryHandler(tryout_handler.remove_create_product, pattern=r"^staff_tryout_remove_\d+$"),
            CallbackQueryHandler(tryout_handler.finish_product_selection, pattern="^staff_tryout_products_done$"),
            CallbackQueryHandler(tryout_handler.confirm_create_tryout, pattern="^staff_tryout_confirm_create$"),
            CallbackQueryHandler(tryout_handler.accept_task, pattern=r"^staff_tryout_accept_\d+$"),
            CallbackQueryHandler(tryout_handler.complete_handoff, pattern=r"^staff_tryout_handoff_\d+$"),
            CallbackQueryHandler(tryout_handler.prompt_pickup, pattern=r"^staff_tryout_pickup_\d+$"),
            CallbackQueryHandler(tryout_handler.show_pickup_overview, pattern=r"^staff_tryout_pickup_back_\d+$"),
            CallbackQueryHandler(tryout_handler.edit_pickup_product, pattern=r"^staff_tryout_pickup_edit_\d+_\d+$"),
            CallbackQueryHandler(tryout_handler.select_pickup_quantity, pattern=r"^staff_tryout_pickup_qty_\d+_\d+_\d+$"),
            CallbackQueryHandler(tryout_handler.clear_pickup_product, pattern=r"^staff_tryout_pickup_clear_\d+_\d+$"),
            CallbackQueryHandler(tryout_handler.fill_pickup_all, pattern=r"^staff_tryout_pickup_all_\d+$"),
            CallbackQueryHandler(tryout_handler.clear_pickup_selection, pattern=r"^staff_tryout_pickup_clearall_\d+$"),
            CallbackQueryHandler(tryout_handler.submit_pickup, pattern=r"^staff_tryout_pickup_submit_\d+$"),
            CallbackQueryHandler(tryout_handler.view_tryout, pattern=r"^staff_tryout_view_\d+$"),

            # Status updates
            CallbackQueryHandler(status_update_handler.initiate_status_change, pattern=r"^staff_status_\d+_"),
            CallbackQueryHandler(status_update_handler.execute_status_change, pattern=r"^staff_execute_status_\d+_"),
            CallbackQueryHandler(status_update_handler.select_fail_reason, pattern=r"^staff_failed_reason_\d+_"),
            CallbackQueryHandler(status_update_handler.confirm_full_cash_collection, pattern=r"^staff_cash_full_\d+$"),
            CallbackQueryHandler(status_update_handler.start_partial_cash_collection, pattern=r"^staff_cash_partial_\d+$"),
            CallbackQueryHandler(status_update_handler.start_no_cash_collection, pattern=r"^staff_cash_none_\d+$"),
            CallbackQueryHandler(status_update_handler.show_reconciliation_session, pattern="^staff_reconcile_session$"),
            CallbackQueryHandler(status_update_handler.submit_reconciliation_all, pattern="^staff_reconcile_submit_all$"),
            CallbackQueryHandler(status_update_handler.start_reconciliation_submit, pattern="^staff_reconcile_submit$"),
            CallbackQueryHandler(status_update_handler.start_reconciliation_transfer, pattern="^staff_reconcile_transfer$"),
            CallbackQueryHandler(cash_collection_handler.start_collection_search, pattern="^staff_cod_collect_menu$"),
            CallbackQueryHandler(cash_collection_handler.show_customer_statement, pattern=r"^staff_cod_customer_\d+$"),
            CallbackQueryHandler(cash_collection_handler.start_full_collection, pattern=r"^staff_cod_collect_full_\d+$"),
            CallbackQueryHandler(cash_collection_handler.start_custom_collection, pattern=r"^staff_cod_collect_custom_\d+$"),
            CallbackQueryHandler(status_update_handler.mark_preparing, pattern=r"^staff_mark_preparing_\d+$"),

            # Bottle return during delivery completion
            CallbackQueryHandler(status_update_handler.confirm_full_bottle_return, pattern=r"^staff_bottles_full_\d+$"),
            CallbackQueryHandler(status_update_handler.start_custom_bottle_return, pattern=r"^staff_bottles_custom_\d+$"),
            CallbackQueryHandler(status_update_handler.skip_bottle_return, pattern=r"^staff_bottles_none_\d+$"),

            # Standalone bottle collection
            CallbackQueryHandler(bottle_collection_handler.show_customer_bottle_statement, pattern=r"^staff_bottle_customer_\d+$"),
            CallbackQueryHandler(bottle_collection_handler.select_address, pattern=r"^staff_bottle_addr_\d+_\d+$"),
            CallbackQueryHandler(bottle_collection_handler.start_collection, pattern=r"^staff_bottle_collect_\d+_\d+$"),
            CallbackQueryHandler(bottle_collection_handler.start_fine, pattern=r"^staff_bottle_fine_\d+_\d+$"),
            # Inline qty picker — replaces the previous typed-quantity step.
            CallbackQueryHandler(bottle_collection_handler.pick_collection_qty, pattern=r"^staff_bottle_qty_\d+_\d+_\d+$"),
            # "Save without note" inline button — submits collection with empty notes.
            CallbackQueryHandler(bottle_collection_handler.save_collection_no_note, pattern=r"^staff_bottle_collect_save_no_note$"),

            # Warehouse bottle accountability (no text input required)
            CallbackQueryHandler(bottle_collection_handler.show_my_accountability, pattern="^staff_bottle_my_accountability$"),

            # Bottle session & transfer (non-conversation callbacks)
            CallbackQueryHandler(bottle_collection_handler.show_pending_transfers, pattern="^staff_bottle_transfers_pending$"),
            CallbackQueryHandler(bottle_collection_handler.receive_transfer_confirm_callback, pattern=r"^staff_transfer_confirm_\d+_\d+$"),

            # History & Stats
            CallbackQueryHandler(history_handler.show_history, pattern="^staff_delivery_history$"),
            CallbackQueryHandler(history_handler.history_pagination, pattern=r"^staff_history_page_\d+$"),
            CallbackQueryHandler(history_handler.show_stats, pattern="^staff_my_stats$"),
            CallbackQueryHandler(history_handler.change_stats_period, pattern=r"^staff_stats_period_"),

            # --- Operator handlers (non-conversation callbacks) ---
            CallbackQueryHandler(operator_orders_pool_view_handler.show_pool, pattern="^staff_op_new_orders$"),
            CallbackQueryHandler(operator_orders_pool_view_handler.view_order_details, pattern=r"^staff_op_view_order_\d+$"),
            CallbackQueryHandler(operator_orders_pool_view_handler.mark_preparing, pattern=r"^staff_op_mark_preparing_\d+$"),
            CallbackQueryHandler(operator_orders_pool_view_handler.pool_pagination, pattern=r"^staff_op_pool_page_\d+$"),
            CallbackQueryHandler(manage_address_handler.show_addresses, pattern=r"^staff_op_addresses_\d+$"),
            CallbackQueryHandler(recent_orders_handler.show_recent_orders, pattern="^staff_recent_orders$"),

            # --- Common handlers ---
            CallbackQueryHandler(profile_handler.show_profile, pattern="^staff_profile$"),
            CallbackQueryHandler(help_handler_instance.show_help, pattern="^staff_help$"),

            # Noop (pagination current page indicator)
            CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"),
        ]

        for handler in callback_handlers:
            self.application.add_handler(handler)

        # --- Operator conversation handlers ---
        create_client_text_pattern = self._menu_text_pattern('staff.menu.create_client')
        search_client_text_pattern = self._menu_text_pattern('staff.menu.search_client')
        create_order_text_pattern = self._menu_text_pattern('staff.menu.create_order')

        # Create User conversation
        create_user_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(create_user_handler.start_create_user, pattern="^staff_create_client$"),
                CallbackQueryHandler(create_user_handler.start_create_user, pattern="^staff_op_create_user$"),
                MessageHandler(
                    filters.Regex(create_client_text_pattern) & ~filters.COMMAND,
                    create_user_handler.start_create_user
                ),
            ],
            states={
                ENTER_PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_phone)
                ],
                ENTER_FIRST_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_first_name)
                ],
                ENTER_LAST_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_last_name)
                ],
                CREATE_USER_LANG: [
                    CallbackQueryHandler(create_user_handler.select_client_language, pattern=r"^staff_op_lang_")
                ],
                CONFIRM_CREATE: [
                    CallbackQueryHandler(create_user_handler.confirm_create, pattern="^staff_op_confirm_create_user$")
                ],
            },
            fallbacks=[
                CommandHandler("cancel", create_user_handler.cancel),
                CallbackQueryHandler(create_user_handler.cancel, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_create_user",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(create_user_conv)

        # Search User conversation
        search_user_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(search_user_handler.start_search, pattern="^staff_search_client$"),
                MessageHandler(
                    filters.Regex(search_client_text_pattern) & ~filters.COMMAND,
                    search_user_handler.start_search
                ),
            ],
            states={
                SEARCH_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, search_user_handler.receive_search_query)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", search_user_handler.cancel),
                CallbackQueryHandler(search_user_handler.cancel, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_search_user",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(search_user_conv)

        # Create Order conversation
        create_order_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(create_order_handler.start_create_order, pattern="^staff_create_order$"),
                CallbackQueryHandler(create_order_handler.start_order_for_client, pattern=r"^staff_op_order_\d+$"),
                MessageHandler(
                    filters.Regex(create_order_text_pattern) & ~filters.COMMAND,
                    create_order_handler.start_create_order
                ),
            ],
            states={
                ORDER_SELECT_CLIENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_order_handler.receive_client_search)
                ],
                ORDER_SELECT_ADDRESS: [
                    CallbackQueryHandler(create_order_handler.select_address, pattern=r"^staff_op_addr_\d+$"),
                ],
                ORDER_SELECT_PRODUCTS: [
                    CallbackQueryHandler(create_order_handler.select_product, pattern=r"^staff_op_product_\d+$"),
                    CallbackQueryHandler(create_order_handler.products_done, pattern="^staff_op_products_done$"),
                ],
                ORDER_SELECT_QUANTITY: [
                    CallbackQueryHandler(create_order_handler.select_quantity, pattern=r"^staff_op_qty_\d+_\d+$"),
                ],
                ORDER_SELECT_PAYMENT: [
                    CallbackQueryHandler(create_order_handler.select_payment, pattern=r"^staff_op_pay_"),
                ],
                ORDER_ENTER_NOTES: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_order_handler.receive_notes),
                    CallbackQueryHandler(create_order_handler.skip_notes, pattern="^staff_op_skip_notes$"),
                ],
                ORDER_CONFIRM_ORDER: [
                    CallbackQueryHandler(create_order_handler.confirm_order, pattern="^staff_op_confirm_order$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", create_order_handler.cancel),
                CallbackQueryHandler(create_order_handler.cancel, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_create_order",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(create_order_conv)

        # Add Address conversation
        add_address_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(manage_address_handler.start_add_address, pattern=r"^staff_op_add_addr_\d+$"),
            ],
            states={
                ENTER_LABEL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_label)
                ],
                ENTER_ADDRESS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_address)
                ],
                ENTER_DISTRICT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_district)
                ],
                ADDR_ENTER_NOTES: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_address_notes)
                ],
                CONFIRM_ADDRESS: [
                    CallbackQueryHandler(manage_address_handler.confirm_address, pattern="^staff_op_confirm_address$")
                ],
            },
            fallbacks=[
                CommandHandler("cancel", manage_address_handler.cancel),
                CallbackQueryHandler(manage_address_handler.cancel, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_add_address",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(add_address_conv)

        create_tryout_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(tryout_handler.start_create_tryout, pattern="^staff_tryout_create$"),
            ],
            states={
                ENTER_TRYOUT_PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_phone)
                ],
                ENTER_TRYOUT_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_name)
                ],
                ENTER_TRYOUT_ADDRESS: [
                    MessageHandler(filters.LOCATION, tryout_handler.receive_create_location),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_address)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", tryout_handler.cancel_create_tryout),
                CallbackQueryHandler(tryout_handler.cancel_create_tryout, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_create_tryout",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(create_tryout_conv)

        # Standalone bottle collection search conversation
        bottle_collection_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bottle_collection_handler.start_collection_search, pattern="^staff_bottle_collect_menu$"),
            ],
            states={
                BOTTLE_COLLECTION_SEARCH_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_collection_search)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", bottle_collection_handler.cancel if hasattr(bottle_collection_handler, 'cancel') else start_handler.cancel),
                CallbackQueryHandler(_exit_to_cash_hub, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_exit_to_main_menu, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_bottle_collection_search",
            conversation_timeout=300,
            allow_reentry=True
        )
        self.application.add_handler(bottle_collection_conv)

        # Driver logs bottles loaded from warehouse
        bottle_loaded_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bottle_collection_handler.start_log_loaded, pattern="^staff_bottle_log_loaded$"),
                CallbackQueryHandler(bottle_collection_handler.start_log_loaded, pattern="^staff_bottle_session_load$"),
                CallbackQueryHandler(bottle_collection_handler.start_log_loaded, pattern="^bottles_start_session$"),
            ],
            states={
                BOTTLES_LOADED_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_loaded)
                ],
                BOTTLE_SESSION_LOADED_QTY_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_loaded)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", start_handler.cancel),
                CallbackQueryHandler(_exit_to_cash_hub, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_exit_to_main_menu, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_bottle_loaded",
            conversation_timeout=300,
            allow_reentry=True,
        )
        self.application.add_handler(bottle_loaded_conv)

        # Driver logs bottles returned to warehouse
        bottle_returned_wh_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bottle_collection_handler.start_return_to_warehouse, pattern="^staff_bottle_return_warehouse$"),
                CallbackQueryHandler(bottle_collection_handler.start_return_to_warehouse, pattern="^staff_bottle_session_return$"),
            ],
            states={
                BOTTLES_RETURNED_WH_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_returned)
                ],
                BOTTLE_SESSION_RETURNED_QTY_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_returned)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", start_handler.cancel),
                CallbackQueryHandler(_exit_to_cash_hub, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_exit_to_main_menu, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_bottle_returned_wh",
            conversation_timeout=300,
            allow_reentry=True,
        )
        self.application.add_handler(bottle_returned_wh_conv)

        # Driver initiates a bottle transfer to another driver
        bottle_transfer_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bottle_collection_handler.start_transfer_bottles, pattern="^staff_bottle_transfer_start$"),
            ],
            states={
                BOTTLE_TRANSFER_DRIVER_SELECT: [
                    CallbackQueryHandler(bottle_collection_handler.receive_transfer_driver_select, pattern=r"^staff_transfer_driver_\d+$"),
                ],
                BOTTLE_TRANSFER_QTY_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_transfer_quantity)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", start_handler.cancel),
                CallbackQueryHandler(_exit_to_main_menu, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_bottle_transfer",
            conversation_timeout=300,
            allow_reentry=True,
        )
        self.application.add_handler(bottle_transfer_conv)

        # Receiver enters a custom confirmed quantity for a pending transfer
        bottle_transfer_confirm_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bottle_collection_handler.start_transfer_custom_confirm, pattern=r"^staff_transfer_custom_\d+$"),
            ],
            states={
                BOTTLE_TRANSFER_CONFIRM_QTY_INPUT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_transfer_custom_confirm)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", start_handler.cancel),
                CallbackQueryHandler(_exit_to_main_menu, pattern="^staff_back_to_main$"),
            ],
            per_chat=True,
            per_user=True,
            name="staff_bottle_transfer_confirm",
            conversation_timeout=300,
            allow_reentry=True,
        )
        self.application.add_handler(bottle_transfer_confirm_conv)

        # Co-driver session join/leave handlers
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.show_joinable_sessions, pattern="^bottles_join_session$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.confirm_join_session, pattern=r"^bottles_join_confirm_\d+$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.execute_join_session, pattern=r"^bottles_join_execute_\d+$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.leave_session, pattern="^bottles_leave_session$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.show_membership_status, pattern="^bottles_membership_status$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.show_invitable_drivers, pattern="^bottles_invite_driver$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.confirm_invite_driver, pattern=r"^bottles_invite_confirm_\d+$")
        )
        self.application.add_handler(
            CallbackQueryHandler(bottle_session_membership_handler.execute_invite_driver, pattern=r"^bottles_invite_execute_\d+$")
        )

        # Keep main-menu back handler after conversations so their fallbacks can run.
        self.application.add_handler(
            CallbackQueryHandler(main_menu_handler, pattern="^staff_back_to_main$"),
            group=1
        )

        # Universal "cancel current flow" handler. Free-text-input prompts that
        # drive a `pending_*_flow` flag (cash collection, reconciliation, COD,
        # standalone bottle collection, tryout pickup) attach a Cancel button
        # via `CommonKeyboards.flow_cancel`. That button clicks here, we wipe
        # every flow flag, and return the user to the cash hub. Without this,
        # `_handle_text_message` keeps intercepting every text update — even
        # reply-keyboard taps — and the user has no way to escape short of
        # typing a value the parser accepts.
        async def _handle_flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
            try:
                await update.callback_query.answer()
            except Exception:
                logger.debug("flow_cancel callback answer failed", exc_info=True)
            for key in (
                'pending_delivery_cash_flow',
                'pending_reconciliation_flow',
                'pending_cod_collection_flow',
                'pending_bottle_collection_flow',
                'tryout_pickup_task_id',
                'tryout_pickup_products',
                'tryout_pickup_state',
            ):
                context.user_data.pop(key, None)
            # C-2: clear the Redis flow marker AND deliver any pool-insertion
            # suggestions deferred while the user was mid-flow. Importing
            # flow_state lazily here keeps the module-level imports of
            # bot.py minimal — flow_state is configured at startup so the
            # call is non-blocking when Redis is reachable.
            from staff_bot.utils import flow_state as _flow_state
            if update and update.effective_user:
                language = context.user_data.get('language') if context else None
                await _flow_state.clear_and_drain(
                    update.effective_user.id, context.bot, language=language
                )
            # Land on the cash hub — every current flow that uses these flags
            # is reachable from there, so it's the least-surprising parent.
            await status_update_handler.show_cash_hub(update, context)

        self.application.add_handler(
            CallbackQueryHandler(_handle_flow_cancel, pattern="^staff_flow_cancel$")
        )

        # Location handler for live location updates
        self.application.add_handler(
            MessageHandler(filters.LOCATION, location_handler.handle_location_update)
        )

        # Catch-all text handler for menu button presses
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)
        )

        logger.info("Staff bot handlers setup completed")

    async def _setup_bot_commands(self):
        """Set up bot command menu"""
        try:
            for lang in ['en', 'uz', 'ru']:
                commands = [
                    BotCommand("start", i18n.get('staff.command.start', lang)),
                    BotCommand("menu", i18n.get('staff.command.menu', lang)),
                    BotCommand("help", i18n.get('staff.command.help', lang)),
                    BotCommand("language", i18n.get('staff.command.language', lang)),
                ]
                await self.application.bot.set_my_commands(commands, language_code=lang)

            # Global fallback commands when Telegram cannot resolve localized scope.
            fallback_commands = [
                BotCommand("start", i18n.get('staff.command.start', 'en')),
                BotCommand("menu", i18n.get('staff.command.menu', 'en')),
                BotCommand("help", i18n.get('staff.command.help', 'en')),
                BotCommand("language", i18n.get('staff.command.language', 'en')),
            ]
            await self.application.bot.set_my_commands(fallback_commands)
            logger.info("Staff bot commands set successfully")
        except Exception as e:
            logger.error(f"Failed to set bot commands: {e}")

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (reply keyboard button presses)"""
        if not context.user_data.get('authenticated'):
            return

        # Delivery COD collection and reconciliation inputs take precedence over menu text.
        cash_flow = context.user_data.get('pending_delivery_cash_flow') or {}
        # Bottle return count during delivery (must check before cash note)
        if cash_flow.get('awaiting_bottle_count'):
            status_update_handler = self._delivery_handlers.get('status_update')
            if status_update_handler:
                await status_update_handler.receive_bottle_count(update, context)
            return
        if cash_flow.get('flow_type') == 'partial' and cash_flow.get('cash_amount') is None:
            status_update_handler = self._delivery_handlers.get('status_update')
            if status_update_handler:
                await status_update_handler.receive_cash_amount(update, context)
            return
        if cash_flow.get('flow_type') in {'partial', 'none'} and cash_flow.get('cash_amount') is not None:
            status_update_handler = self._delivery_handlers.get('status_update')
            if status_update_handler:
                await status_update_handler.receive_cash_note(update, context)
            return
        if context.user_data.get('pending_reconciliation_flow'):
            status_update_handler = self._delivery_handlers.get('status_update')
            if status_update_handler:
                await status_update_handler.receive_reconciliation_declared_cash(update, context)
            return
        cod_collection_flow = context.user_data.get('pending_cod_collection_flow') or {}
        if cod_collection_flow:
            cash_collection_handler = self._delivery_handlers.get('cash_collection')
            if cash_collection_handler:
                if cod_collection_flow.get('amount') is None:
                    await cash_collection_handler.receive_collection_amount(update, context)
                else:
                    await cash_collection_handler.receive_collection_note(update, context)
            return

        # Bottle collection and fine flows (standalone). Collection's qty step
        # is now button-driven (see DeliveryKeyboards.bottle_collection_qty_picker)
        # so text is only meaningful for the optional note step — when
        # `flow['quantity']` is set we route to `receive_collection_note`,
        # otherwise we swallow the text to avoid leaking it to the menu router
        # while the picker is on-screen.
        bottle_flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if bottle_flow:
            bottle_handler = self._delivery_handlers.get('bottle_collection')
            if bottle_handler:
                action = bottle_flow.get('action')
                if action == 'collect':
                    if bottle_flow.get('quantity') is None:
                        # Picker still on screen — ignore text input.
                        return
                    await bottle_handler.receive_collection_note(update, context)
                    return
                elif action == 'fine':
                    if bottle_flow.get('fine_quantity') is None:
                        await bottle_handler.receive_fine_bottle_qty(update, context)
                    elif bottle_flow.get('fine_amount') is None:
                        await bottle_handler.receive_fine_amount(update, context)
                    else:
                        await bottle_handler.receive_fine_note(update, context)
                    return

        if context.user_data.get('tryout_pickup_task_id'):
            tryout_handler = self._delivery_handlers.get('tryouts')
            if tryout_handler:
                await tryout_handler.receive_pickup_quantities(update, context)
            return

        text = update.message.text.strip()
        language = await self._language_handler._get_language(update, context)

        # Map reply keyboard text to actions
        # These match the text in MenuKeyboards.main_menu()
        menu_actions = {
            i18n.get('staff.menu.new_orders', language): 'staff_new_orders_unified',
            i18n.get('staff.menu.active_deliveries', language): 'staff_active_deliveries',
            i18n.get('staff.menu.tryouts', language): 'staff_tryouts_hub',
            i18n.get('staff.menu.cash', language): 'staff_cash_hub',
            i18n.get('staff.menu.profile', language): 'staff_profile',
            i18n.get('staff.menu.settings', language): 'staff_settings',
            i18n.get('staff.menu.help', language): 'staff_help',
        }

        # Strip emoji prefix when matching
        clean_text = text
        for prefix_len in [2, 3, 4]:
            stripped = text[prefix_len:].strip() if len(text) > prefix_len else text
            if stripped in menu_actions:
                clean_text = stripped
                break

        if clean_text in menu_actions:
            action = menu_actions[clean_text]
            # Route to actual handlers
            handler_map = {
                # Unified entry points
                'staff_new_orders_unified': self._route_new_orders,
                'staff_tryouts_hub': self._delivery_handlers['tryouts'].show_hub,
                'staff_cash_hub': self._delivery_handlers['status_update'].show_cash_hub,
                # Delivery
                'staff_active_deliveries': self._delivery_handlers['active_delivery'].show_active_deliveries,
                # Common
                'staff_profile': self._common_handlers['profile'].show_profile,
                'staff_settings': None,  # Handled by language_handler callback
                'staff_help': self._common_handlers['help'].show_help,
            }

            handler_func = handler_map.get(action)
            if handler_func:
                await handler_func(update, context)
            elif action == 'staff_settings':
                await self._language_handler.language_menu(update, context)
        else:
            # Unknown text input
            await main_menu_handler(update, context)

    async def _help_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        language = await self._language_handler._get_language(update, context)
        staff_roles = context.user_data.get('staff_roles', [])

        help_text = i18n.get('staff.help.text', language)

        if 'delivery_driver' in staff_roles:
            help_text += "\n\n" + i18n.get('staff.help.delivery', language)
        if 'operator' in staff_roles:
            help_text += "\n\n" + i18n.get('staff.help.operator', language)

        await update.message.reply_text(help_text)

    def run(self):
        """Run the bot"""
        try:
            if config.telegram.webhook_url:
                logger.info(f"Starting staff bot with webhook: {config.telegram.webhook_url}")
                self.application.run_webhook(
                    listen=config.telegram.webhook_listen,
                    port=config.telegram.webhook_port,
                    webhook_url=config.telegram.webhook_url,
                )
            else:
                logger.info("Starting staff bot with polling mode")
                self.is_running = True
                self.application.run_polling(
                    poll_interval=config.telegram.poll_interval,
                    timeout=config.telegram.polling_timeout,
                    bootstrap_retries=config.telegram.bootstrap_retries,
                    allowed_updates=None,
                    drop_pending_updates=config.telegram.drop_pending_updates,
                )
        except Exception as e:
            logger.error(f"Error running staff bot: {e}", exc_info=True)
            raise

    async def cleanup(self):
        """Cleanup resources"""
        try:
            logger.info("Cleaning up staff bot resources...")
            await webhook_server.stop()

            # Close the persistent backend HTTP client. Counterpart to
            # api_client.start() in initialize(). Closing here (and only here)
            # is the reason `async with api_client as client:` in handlers no
            # longer tears down the shared client on every request.
            try:
                await api_client.aclose()
            except Exception:
                logger.debug("Failed to close persistent api_client", exc_info=True)

            if self.token_manager:
                await self.token_manager.close()

            if db_manager.is_connected:
                await db_manager.disconnect()

            self.is_running = False
            logger.info("Staff bot cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    async def stop(self):
        """Stop the bot gracefully"""
        logger.info("Stopping staff bot...")
        if self.application:
            await self.application.stop()
        await self.cleanup()


# Global bot instance
bot = StaffBot()


def main():
    """Main entry point"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}")
        if bot.application and hasattr(bot.application, 'stop'):
            try:
                asyncio.create_task(bot.stop())
            except RuntimeError:
                sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.initialize())
        bot.run()
    except KeyboardInterrupt:
        logger.info("Staff bot stopped by user")
    except Exception as e:
        logger.error(f"Staff bot crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    main()
