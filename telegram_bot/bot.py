"""
Main Telegram Bot Application
Comprehensive water business bot with full feature integration
"""
import asyncio
import functools
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
    Application, ApplicationHandlerStop, ChatMemberHandler, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes, TypeHandler
)
from telegram.error import TelegramError
from shared.telegram_request import ResilientHTTPXRequest
from shared.telegram_update_processor import PerChatSerialUpdateProcessor

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
from support_capture import capture_support_message
from webhook_server import webhook_server
from token_manager import TokenManager
from handlers import (
    main_menu_handler, language_handler,
    product_handlers, order_handlers, subscription_handlers,
    profile_handlers, loyalty_handlers, admin_handlers,
    support_handlers, support_flow_handlers, payment_handlers, bottle_handlers,
    quick_order_handlers, agent_order_handlers, reorder_reminder_handlers,
)
# Import conversation states directly (they are module-level constants)
from handlers.profile import (
    SELECT_LANGUAGE, PHONE, LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP,
    ADDRESS_LOCATION, ADDRESS_TITLE, ADDRESS_REGION, ADDRESS_DISTRICT,
    ADDRESS_STREET, ADDRESS_BUILDING, ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
    WEB_LINK_CONFIRM, WEB_LINK_PENDING_KEY, WEB_LINK_GREETING_KEY, PHONE_OTP_ARMED_AT,
)
from utils import (
    error_handler, rate_limiter, user_middleware, get_auth_token, is_awaiting_location_stale,
    otp_code_in, OtpShapedFilter,
)
from keyboards import MenuKeyboards, PRODUCT_PAGE_PATTERN
from menu_keyboard import MENU_INSTALLER_GROUP, ensure_menu_keyboard_middleware, send_menu_keyboard
from menu_tap import MenuTapFilter
from flow_exit import (
    FLOW_EXIT_GROUP, FREE_TEXT_FILTER, ITEM_MANAGEMENT_KEYS, PHONE_FLOW_KEYS, SUBSCRIPTION_CREATION_KEYS,
    UPDATE_ITEM_KEYS, FlowExitHandler, pending_menu_tap,
)

logger = logging.getLogger('bot')


class WaterBusinessBot:
    """Main bot application class"""

    def __init__(self):
        self.application: Optional[Application] = None
        self.is_running = False
        self.user_repository = BotUserRepository(db_manager)
        self.token_manager: Optional[TokenManager] = None

    @staticmethod
    def _consumes(callback):
        """Wrap a conversation callback so the update STOPS at the conversation.

        PTB dispatches at most one handler per GROUP and then walks on to the
        NEXT group. Every ConversationHandler here lives in group -2 while the
        free-text catch-all `_handle_text_message` sits in group 0, so an answer
        typed INSIDE a flow was processed twice: once by the step that asked for
        it, and once by the catch-all, which silently filed it in the admin
        Support Inbox as an unsolicited customer message. Production,
        2026-08-20 23:07:12 (+05), telegram user 251067721: one sentence of
        delivery instructions both saved the address AND opened a support
        ticket. Registration leaked the same way — the typed phone number and
        the LIVE SMS one-time code were filed as two tickets per signup.

        `ConversationHandler.handle_update` catches ApplicationHandlerStop,
        takes `exception.state` as the new conversation state and re-raises it,
        so this changes only WHO ELSE sees the update: the value the callback
        returns still drives the state machine exactly as before.

        Wrap ONLY callbacks whose update no later group may act on. In
        particular NOT the `ConversationHandler.TIMEOUT` handlers — PTB
        dispatches those itself and warns that ApplicationHandlerStop there has
        no effect — and not steps whose update kind no group-0 handler claims
        (CONTACT and the callback patterns that appear only inside a
        conversation), because stopping dispatch there would buy nothing and
        would silence the group -1 callback logger for no reason. Also NOT the
        address conversation's entry-point LOCATION handler — that one's stop
        decision is conditional, not unconditional, so it manages its own
        `ApplicationHandlerStop` in `_route_address_location_entry` instead.
        """
        @functools.wraps(callback)
        async def _stop_after_this_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
            raise ApplicationHandlerStop(await callback(update, context))

        return _stop_after_this_conversation

    def _route_address_location_entry(self, callback):
        """Entry point for a bare LOCATION update onto the address conversation.

        `filters.LOCATION` matches EVERY pin a customer shares, so this entry
        point cannot rely on "no conversation is active" the way a plain
        `ConversationHandler` normally would — a pin the bot never asked for
        looks identical, from PTB's perspective, to one it did (the
        zero-address-checkout keyboard, for instance, shows the pin prompt
        WITHOUT ever starting this conversation). Decide from an explicit
        marker instead:

        * `awaiting_location` in `context.user_data` — the bot just asked for
          this pin (`utils.arm_location_request`, called at every site that
          sends `ProfileKeyboards.location_request(...)`, and cleared at every
          teardown site by `ProfileHandlers.clear_address_flow_keys` so it
          never outlives the flow that armed it). Consume the marker and hand
          off to `location_received` — UNLESS the arming has gone stale (see
          below), in which case treat it as though it were never armed.
        * `temp_address_data` in `context.user_data` — an address draft is
          already mid-flow (a re-entrant pin, which `allow_reentry=True`
          routes back through THIS entry point rather than
          `states[ADDRESS_LOCATION]`). Also hand off to `location_received`.
        * Neither — a spontaneous pin nobody asked for. Route it through the
          same guarded capture every other unsolicited attachment uses
          (`_capture_support_with_guards`) and end with no conversation
          started, so it is never mistaken for an address later.

        `filters.LOCATION` matches `edited_message` too (a live-location share
        emits one of those per tick), which carries no `update.message` at
        all — bail out before touching either branch, or a null message falls
        into `capture_support_message` and posts one junk
        `message_type=unsupported` row per tick.

        Staleness: `arm_location_request` is the ONLY site that sets
        `awaiting_location`, and the zero-address-checkout call site never
        starts a `ConversationHandler` — it just shows a keyboard and waits.
        If the customer neither pins nor cancels, no timeout ever fires and
        `clear_address_flow_keys` is never reached, so the flag would
        otherwise survive for the life of the process. Treat an arming older
        than `utils.AWAITING_LOCATION_STALE_MINUTES` as not armed at all —
        same pattern `handlers/support.py::_is_stale` uses for the concern
        flow.

        Product ruling 2026-08-25: a pin only belongs to the address flow when
        the bot actually asked for one; an unprompted pin is a support
        message. See tests/telegram_bot/test_support_attachment_dispatch.py.

        Every branch raises `ApplicationHandlerStop`: this handler fully owns
        the update whichever way it decides.
        """
        bot = self

        @functools.wraps(callback)
        async def _route(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.message is None:
                # BARE stop, never `ApplicationHandlerStop(ConversationHandler.END)`:
                # PTB reads `exception.state` (conversationhandler.py:853) and
                # `_update_state` deletes the conversation on END. With
                # `allow_reentry=True` this entry point runs mid-flow too, so
                # passing END here let ONE live-location tick silently kill a
                # customer's open address form. `state=None` no-ops instead,
                # and dispatch still stops.
                raise ApplicationHandlerStop()

            armed = context.user_data.pop('awaiting_location', False)
            armed_at = context.user_data.pop('awaiting_location_at', None)

            # The durable half, for the pin that arrives after the process that
            # asked for it died. `arm_location_request` dual-writes this to
            # `users.bot_state`; without it a deploy in the gap made `armed`
            # False and this handler filed the customer's home coordinates as an
            # unsolicited support message — silently — while checkout stayed
            # stuck behind the address they thought they had just added.
            #
            # Same staleness window either way, so a durable marker cannot
            # reopen address creation for an unrelated pin weeks later.
            # tests/telegram_bot/test_armed_prompts_survive_a_restart.py
            if not armed:
                try:
                    durable = await bot.user_repository.get_user_state(
                        update.effective_user.id
                    )
                except Exception as state_error:
                    logger.warning("Could not read the pin arming: %s", state_error)
                    durable = {}
                if durable.get('awaiting_location_at'):
                    armed = True
                    armed_at = durable.get('awaiting_location_at')
                    await bot.user_repository.forget_pin_prompt(
                        update.effective_user.id
                    )

            if armed and is_awaiting_location_stale(armed_at):
                armed = False
            mid_flow = 'temp_address_data' in context.user_data
            if armed or mid_flow:
                raise ApplicationHandlerStop(await callback(update, context))

            try:
                await bot._capture_support_with_guards(update, context)
            except Exception as e:
                logger.error(f"Error handling spontaneous location message: {e}")
            # BARE stop, same reasoning as the null-message guard above: this
            # branch is reachable only while `temp_address_data` happens to be
            # absent, an accident of the current marker rather than a real
            # guarantee no conversation is open. Passing END would delete an
            # open conversation on the day that accident stops holding.
            raise ApplicationHandlerStop()

        return _route

    @staticmethod
    def _flow_timeout(message_key: str, *state_keys: str, offer_menu: bool = True):
        """Build the ``ConversationHandler.TIMEOUT`` callback for one flow.

        ``conversation_timeout`` is not self-announcing: when the timer fires
        PTB looks for handlers under the TIMEOUT key and, finding none, ends
        the conversation in TOTAL SILENCE. The customer is left on a prompt
        whose buttons are dead, and the flow's keys survive in ``user_data``
        for the next flow to trip over. Registration was the worst of the five
        that did this — 300s is well inside normal Uzbek SMS latency, so a
        customer waiting for their code was dropped mid-signup and never told,
        and the code they eventually pasted was filed as a support ticket by
        the group-0 catch-all because ``awaiting_otp`` was still set.

        One factory rather than five copies: "say so, drop the flow's keys,
        end" is one rule, and the per-flow parts are its arguments. It lives
        here, beside the registrations, because these five flows have no
        state to unwind beyond those keys. ``address_conversation`` keeps its
        own handler in ``handlers/profile.py`` — its copy depends on whether
        the pin step already saved an address, which only that module knows.

        Deliberately NOT wrapped in :meth:`_consumes`: PTB dispatches TIMEOUT
        handlers itself and warns that ApplicationHandlerStop has no effect
        there.
        """
        async def _announce_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
            try:
                user_id = update.effective_user.id
                language = await i18n.get_user_language(user_id)

                for key in state_keys:
                    context.user_data.pop(key, None)

                logger.info(
                    "Conversation timed out for user %s; cleared %s",
                    user_id, list(state_keys),
                )

                text = i18n.get(message_key, language)
                # The timeout update is synthetic — it carries whatever the
                # customer's last real one was — so the reply target is
                # derived rather than assumed.
                query = update.callback_query
                if offer_menu:
                    chat = update.effective_chat
                    await send_menu_keyboard(
                        context.bot, chat.id if chat is not None else user_id, user_id, text, language
                    )
                # A half-registered customer has no menu to go back to, and the
                # step they abandoned left a request_contact REPLY keyboard on
                # their screen; clearing it is the only honest thing to show.
                elif query is not None and query.message is not None:
                    await query.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
                elif update.message is not None:
                    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
                else:
                    await context.bot.send_message(
                        chat_id=user_id, text=text, reply_markup=ReplyKeyboardRemove()
                    )

            except Exception as e:
                logger.error("Error announcing conversation timeout (%s): %s", message_key, e)

            return ConversationHandler.END

        return _announce_timeout

    async def initialize(self):
        """Initialize bot and all dependencies"""
        try:
            log_bot_startup_info()
            logger.info("Initializing Water Business Bot...")

            # Initialize database connection
            await db_manager.connect()

            # Load translations
            await i18n.load_translations()

            # Initialize the persistent backend HTTP client. Owned by the bot
            # lifecycle so all handlers reuse ONE httpx.AsyncClient with a
            # shared connection pool — the same fix staff_bot already had.
            # This is a prerequisite for concurrent update processing, not
            # just an optimisation: `api_client` is a module-level singleton,
            # so the previous build-per-context/close-on-exit pattern would
            # let one handler close the client another was still using.
            await api_client.start()

            # Note: api_client is still used as an async context manager in
            # handlers; __aenter__ now just ensures the shared client exists.

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

            # Per-chat serial, cross-chat concurrent. PTB's default is to
            # process every update one by one, so a single slow handler
            # blocks every other customer — most visibly the ~45s wait in
            # `handlers/orders.py::confirm_order` while Click marking-code
            # fiscalization settles, which froze the WHOLE bot for everyone.
            #
            # A bare `.concurrent_updates(True)` is not safe here: PTB warns
            # against it with stateful handlers and this bot runs eight
            # ConversationHandlers (registration, address, phone verification,
            # and the subscription flows). Checkout is NOT one of them — it is
            # a plain CallbackQueryHandler in group 0, which is precisely why
            # `utils.arm_location_request` has to exist; do not let this
            # comment imply otherwise. PerChatSerialUpdateProcessor keeps each
            # customer's updates ordered while unblocking everyone else — and
            # it also removes the head-of-line queuing that
            # `handlers/callback_dedup.py` documents as the cause of
            # duplicate messages on an impatient double-tap.
            self.application = (
                Application.builder()
                .token(config.telegram.bot_token)
                .request(request)
                .get_updates_request(get_updates_request)
                .concurrent_updates(
                    PerChatSerialUpdateProcessor(
                        max_concurrent_updates=int(
                            os.environ.get("TELEGRAM_BOT_CONCURRENT_UPDATES", "16")
                        )
                    )
                )
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
            # ...and give it to the API client, which replaces a customer token the backend refuses
            api_client.token_manager = self.token_manager

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

            logger.info("Bot initialization completed successfully")

        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def _setup_handlers(self):
        """Set up all bot handlers.

        Includes the two dispatcher middlewares. They used to be registered
        in `initialize()` instead, which split the wiring across two methods:
        anything that builds this bot from `_setup_handlers()` alone — the
        test harness in tests/telegram_bot/ptb_harness.py included — got an
        Application with NO callback-dedup guard, so a double-tap regression
        was invisible to every dispatcher test. Registered here so there is
        one expression of the wiring rather than two that can drift.
        """

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

        # Quantity-screen middleware: anything but a tap on the live cart
        # quantity screen or a typed quantity — another button, an older
        # bubble, a command, words, a photo — closes the window in which a
        # TYPED number sets that screen's cart line. Behind the dedup guard (a
        # dropped duplicate navigates nowhere) and ahead of every conversation
        # and handler (so the window is shut before whatever the customer did
        # arms its own prompt). Never stops dispatch.
        # See handlers.quantity_screen.
        from handlers.quantity_screen import leave_quantity_screen_middleware
        self.application.add_handler(
            TypeHandler(Update, leave_quantity_screen_middleware), group=-4
        )
        logger.info("Quantity-screen middleware installed!")

        # Lazy menu-keyboard installer (spec 2026-10-21 §1, "Existing customers, no
        # broadcast"): an inline tap from a registered customer whose screen lacks the
        # current bottom keyboard sends it, silently, through send_menu_keyboard. BEFORE
        # dispatch, so a phone or location keyboard the handler sends afterwards still
        # wins. Never stops dispatch; never answers typed text. See menu_keyboard.py.
        self.application.add_handler(
            TypeHandler(Update, ensure_menu_keyboard_middleware), group=MENU_INSTALLER_GROUP
        )
        logger.info("Menu-keyboard installer installed!")

        # Flow exit (spec 2026-10-21 §2): a bottom-keyboard label (as it reads now, or
        # as the customer still has it installed), /start, /menu, /help, /language or a
        # legacy menu callback ends every open flow but
        # signup and disarms the saved prompts. After the quantity-screen
        # middleware and BEFORE every conversation (group -2), so no step can
        # take a label as its answer. Never stops dispatch: group 0 then draws
        # the destination once. See flow_exit.py.
        self.application.add_handler(FlowExitHandler(), group=FLOW_EXIT_GROUP)
        logger.info("Flow exit installed!")

        # Telegram reachability (spec 2026-10-09 §7.2 a, b). Telegram announces a block or a
        # return as a `my_chat_member` update; its handler reports it from a group of its own (1),
        # where no group-0 handler can shadow it and it sits behind no text catch-all. The
        # recovery middleware covers the `my_chat_member` updates a restart drops: any other
        # update from a customer the backend still holds as blocked reports them reachable. It
        # sits right after the debug logger, sees every update and never stops dispatch.
        from handlers.chat_member import bot_membership_changed, report_recovery_middleware
        self.application.add_handler(
            TypeHandler(Update, report_recovery_middleware), group=-9
        )
        self.application.add_handler(
            ChatMemberHandler(bot_membership_changed, ChatMemberHandler.MY_CHAT_MEMBER), group=1
        )
        logger.info("Telegram reachability reporting installed!")


        # Command handlers
        self.application.add_handler(CommandHandler("menu", main_menu_handler))
        self.application.add_handler(CommandHandler("help", support_handlers.support_menu))
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
            # Previous / Next on a category's product list. The pattern comes
            # from keyboards.py, next to the builder that renders the buttons
            # and the parser that reads them back, so the shape of this
            # callback is decided in exactly one place. Registered BEFORE
            # `^product_` only for readability — the two cannot overlap.
            CallbackQueryHandler(product_handlers.product_page_handler, pattern=PRODUCT_PAGE_PATTERN),
            CallbackQueryHandler(product_handlers.product_details, pattern="^product_"),
            # `\d+` is load-bearing, not tidiness: `product_details` reads
            # segment 3 as an integer, so a bare `^back_to_product_` prefix
            # claimed a namespace this handler cannot parse. The subscription
            # quantity screen's `back_to_product_selection` landed here and
            # died inside `int('selection')` — and it landed here even once a
            # conversation claimed it, because PTB walks EVERY group.
            CallbackQueryHandler(product_handlers.product_details, pattern=r"^back_to_product_\d+$"),
            CallbackQueryHandler(product_handlers.add_to_cart, pattern="^add_to_cart_"),
            CallbackQueryHandler(product_handlers.quantity_handler, pattern="^qty_"),
            CallbackQueryHandler(product_handlers.cart_handler, pattern="^cart_"),
            CallbackQueryHandler(product_handlers.show_cart, pattern="^back_to_cart$"),

            # Quick Order callbacks (Products menu top + order-history reorder)
            CallbackQueryHandler(quick_order_handlers.handle_repeat_last, pattern="^quick_repeat_last$"),
            CallbackQueryHandler(quick_order_handlers.handle_usual, pattern="^quick_usual$"),
            CallbackQueryHandler(quick_order_handlers.handle_reorder_from_history, pattern="^reorder_\\d+$"),
            # The open-order notice's "continue" buttons. Before `^order_` below, which would claim order_water_go.
            CallbackQueryHandler(quick_order_handlers.order_water_continue, pattern="^order_water_go$"),
            CallbackQueryHandler(quick_order_handlers.reorder_continue, pattern=r"^reorder_go_\d+$"),

            # Order callbacks
            CallbackQueryHandler(order_handlers.orders_menu, pattern="^menu_orders$"),
            CallbackQueryHandler(order_handlers.order_details, pattern="^order_"),
            # Reward-in-checkout handlers MUST precede the broad "^checkout" catch-all
            # below, or it would swallow these checkout_* callbacks.
            CallbackQueryHandler(order_handlers.checkout_choose_reward, pattern="^checkout_choose_reward$"),
            CallbackQueryHandler(order_handlers.checkout_apply_reward, pattern="^checkout_apply_reward_\\d+$"),
            CallbackQueryHandler(order_handlers.checkout_remove_reward, pattern="^checkout_remove_reward$"),
            CallbackQueryHandler(order_handlers.checkout_change_address, pattern="^checkout_change_address$"),
            CallbackQueryHandler(order_handlers.back_to_payment, pattern=r"^back_to_payment(_\d+)?$"),
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
            CallbackQueryHandler(subscription_handlers.skip_delivery, pattern=r"^skip_sub_\d+$"),
            CallbackQueryHandler(subscription_handlers.view_billing_history, pattern=r"^billing_history_\d+$"),

            # Subscription item management
            CallbackQueryHandler(subscription_handlers.manage_subscription_items, pattern=r"^manage_items_\d+$"),
            CallbackQueryHandler(subscription_handlers.remove_item_confirm, pattern="^remove_item_"),

            # Subscription editing
            CallbackQueryHandler(subscription_handlers.edit_subscription_menu, pattern=r"^edit_sub_\d+$"),
            CallbackQueryHandler(subscription_handlers.change_frequency, pattern=r"^change_frequency_\d+$"),
            CallbackQueryHandler(subscription_handlers.change_payment_method_menu, pattern=r"^change_payment_\d+$"),

            # Statistics and logs
            CallbackQueryHandler(subscription_handlers.view_subscription_statistics, pattern="^subscription_statistics$"),
            CallbackQueryHandler(subscription_handlers.view_subscription_logs, pattern=r"^view_logs_\d+$"),
            CallbackQueryHandler(subscription_handlers.retry_failed_billing, pattern=r"^retry_billing_\d+$"),

            # Profile callbacks
            CallbackQueryHandler(profile_handlers.profile_menu, pattern="^menu_profile$"),
            CallbackQueryHandler(profile_handlers.phone_verification_menu, pattern="^phone_verification$"),
            CallbackQueryHandler(profile_handlers.notification_settings, pattern="^notification_settings$"),
            CallbackQueryHandler(
                profile_handlers.toggle_delivery_telegram_status_notifications,
                pattern="^toggle_delivery_telegram_status_(on|off)$",
            ),
            CallbackQueryHandler(
                profile_handlers.toggle_reorder_reminders,
                pattern="^toggle_reorder_reminders_(on|off)$",
            ),
            # add_phone_number is now handled by phone_verification_handler ConversationHandler
            CallbackQueryHandler(profile_handlers.verify_phone_number, pattern="^verify_phone_number$"),
            CallbackQueryHandler(profile_handlers.cancel_phone_otp, pattern="^phone_otp_cancel$"),
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
            CallbackQueryHandler(bottle_handlers.show_bottle_history, pattern=r"^bottle_history_\d+_\d+$"),

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

            # Concern flow off the delivered-summary "Report an issue" button.
            # No conflict with the ^order_ / ^checkout / ^cancel_ / ^menu_support
            # prefixes above ("report_issue_" / "support_cancel" match none of them).
            CallbackQueryHandler(support_flow_handlers.start_order_issue_report, pattern=r"^report_issue_\d+$"),
            CallbackQueryHandler(support_flow_handlers.cancel_issue_report, pattern="^support_cancel$"),

            # Sales-agent order proposal pushed by
            # telegram_bot/webhook_server.py::agent_order_proposed_handler.
            # No conflict with the ^order_ / ^cancel_order_ / ^confirm_order
            # prefixes registered above: none of them is a prefix of
            # "agent_order_". `\d+$` is load-bearing — the handler parses the
            # trailing segment with int().
            CallbackQueryHandler(agent_order_handlers.confirm, pattern=r"^agent_order_confirm_\d+$"),
            CallbackQueryHandler(agent_order_handlers.decline, pattern=r"^agent_order_decline_\d+$"),

            # Reorder-reminder buttons pushed by
            # telegram_bot/webhook_server.py::reorder_reminder_handler and built by
            # keyboards.ReorderReminderKeyboards. No registered pattern is a prefix of "rr_".
            # `\d+$` is load-bearing: the handler parses the trailing segment with int().
            CallbackQueryHandler(reorder_reminder_handlers.later, pattern=r"^rr_later_\d+$"),
            CallbackQueryHandler(reorder_reminder_handlers.stop, pattern=r"^rr_stop_\d+$"),

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
        registration_timeout = self._flow_timeout(
            'telegram.registration.flow_timed_out',
            *PHONE_FLOW_KEYS, 'pending_link_phone', WEB_LINK_GREETING_KEY,
            offer_menu=False,
        )

        # While a web link waits for Yes, the timeout says the LINK expired and
        # drops it (spec 2026-10-19 D15b). Registration's own "Send /start" copy
        # would put an unknown Telegram on the language card, whose tap creates
        # the row that then blocks the link.
        async def _registration_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if WEB_LINK_PENDING_KEY in (context.user_data or {}):
                return await profile_handlers.web_link_timeout(update, context)
            return await registration_timeout(update, context)

        registration_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", profile_handlers.start_registration_new),
                # CallbackQueryHandler(profile_handlers.start_registration_new, pattern="^/start$")
            ],
            states={
                # `^set_language_` is ALSO registered standalone in group 0
                # (language_handler.set_language, for changing language later).
                # Telegram accepts exactly ONE answerCallbackQuery per query, so
                # while both ran a brand-new customer's very first tap was
                # answered by the language-CHANGE handler telling them they
                # already use the language they had just picked.
                SELECT_LANGUAGE: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.language_selection),
                        pattern="^set_language_",
                    )
                ],
                PHONE: [
                    MessageHandler(
                        filters.CONTACT,
                        self._consumes(profile_handlers.phone_received),
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.phone_text_received),
                    )
                ],
                # Account linking states
                LINK_ACCOUNT_CONFIRM: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.link_account_confirm),
                        pattern="^link_",
                    )
                ],
                LINK_ACCOUNT_OTP: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.link_account_otp),
                    )
                ],
                # Registration OTP captured in-conversation (Task 12). /cancel
                # and /start fallbacks below now apply during OTP entry.
                REGISTER_OTP: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.register_otp_received),
                    )
                ],
                # The web-link confirm card (spec 2026-10-19 R4). Consumed: the
                # default group answers these same buttons once the conversation
                # is gone (web_link_step_expired below), and must not see a live tap.
                WEB_LINK_CONFIRM: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.web_link_yes),
                        pattern=r"^weblink_yes$",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.web_link_cancel),
                        pattern=r"^weblink_no$",
                    ),
                ],
                # 300 seconds is well inside normal Uzbek SMS latency, so this
                # is the flow the silent timeout hurt most. `awaiting_otp` and
                # friends MUST go with it: they are read by the group-0 text
                # catch-all, which would otherwise send the next thing this
                # customer types to the phone-verification endpoint. No main
                # menu — they are not registered yet — and ReplyKeyboardRemove
                # clears the request_contact keyboard the phone step left up.
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, _registration_timeout),
                    CallbackQueryHandler(_registration_timeout),
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

        # The address form's own reply-keyboard labels: "Enter manually" on its
        # location keyboard, "Re-enter address" on the geocode-retry one, and the
        # Cancel row both carry. One filter object each, shared by the address
        # conversation, which answers them, and the phone conversation below,
        # which must leave them alone.
        address_manual_entry_tap = MenuTapFilter(
            'telegram.address.enter_manually_button',
            'telegram.address.reenter_manually_button',
        )
        cancel_tap = MenuTapFilter('telegram.cancel')
        address_keyboard_tap = address_manual_entry_tap | cancel_tap

        # Phone verification conversation - for adding/updating phone number and name
        # Registered BEFORE the address conversation, in the same group: PTB runs
        # one handler per group, and the address conversation's Cancel entry point
        # would otherwise claim the Cancel row of the share-contact keyboard while
        # this flow is on its phone step.
        #
        # The other way round, both forms can be open at once: an older inline
        # "Add address" button (subscriptions, checkout) opens the address form
        # over this one. So no text handler here takes the address keyboard's
        # labels (`address_keyboard_tap`) as its answer: they fall through to the
        # address conversation. Taken here, "Enter manually" was saved as the
        # customer's NAME, and Cancel ended the code step instead of the form.
        # The phone step keeps Cancel: its own keyboard has that row.
        phone_verification_timeout = self._flow_timeout(
            'telegram.phone.verification_flow_timed_out',
            *PHONE_FLOW_KEYS,
        )
        phone_verification_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(profile_handlers.add_phone_number, pattern="^add_phone_number$"),
            ],
            states={
                profile_handlers.PHONE_VERIFY_PHONE: [
                    # The share-contact keyboard's own Cancel row. First, so the
                    # typed-number handler below never reads it as a number.
                    MessageHandler(
                        filters.TEXT & cancel_tap,
                        self._consumes(profile_handlers.cancel_phone_verification),
                    ),
                    MessageHandler(
                        filters.CONTACT,
                        self._consumes(profile_handlers.phone_verify_contact_received),
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND & ~address_manual_entry_tap,
                        self._consumes(profile_handlers.phone_verify_text_received),
                    ),
                ],
                # A typed number is saved only once its SMS code is verified.
                # Only a code answers this step (spec 2026-10-21 §2): any other
                # text ends it and, NOT wrapped in `_consumes`, walks on to the
                # group-0 catch-all like any other message. A wrong code still
                # re-prompts here. The address keyboard's labels are neither: they
                # leave the step as it is and go to the address conversation.
                profile_handlers.PHONE_VERIFY_OTP: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND & OtpShapedFilter() & ~address_keyboard_tap,
                        self._consumes(profile_handlers.phone_verify_otp_received),
                    ),
                    MessageHandler(
                        FREE_TEXT_FILTER & ~address_keyboard_tap, profile_handlers.end_phone_code_step,
                    ),
                ],
                profile_handlers.PHONE_VERIFY_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND & ~address_keyboard_tap,
                        self._consumes(profile_handlers.phone_verify_name_received),
                    ),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, phone_verification_timeout),
                    CallbackQueryHandler(phone_verification_timeout),
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

        # Address input conversation - Enhanced flow with manual entry support
        address_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                        self._consumes(profile_handlers.add_address),
                        pattern="^add_new_address(_checkout)?$",
                    ),
                # A pin can arrive BEFORE the flow has "started" in the
                # ConversationHandler sense, because zero-address checkout
                # shows the pin keyboard without ever entering this
                # conversation. `allow_reentry=True` below means THIS entry
                # point — not the states block's copy further down — is what
                # actually runs for every location update this conversation
                # ever sees, tap or no tap. `_route_address_location_entry`
                # decides per-update whether the bot actually asked for this
                # pin (address flow) or it is spontaneous (support ticket) —
                # see that method's docstring.
                #
                # filters.VENUE is explicit here rather than relied-upon: a
                # venue share ALSO sets `message.location` (Telegram keeps
                # that field for backward compatibility), which is an
                # undocumented external guarantee, not a contract PTB or we
                # control. Matching VENUE directly means venue capture keeps
                # working even if Telegram ever stops duplicating the field.
                MessageHandler(
                    filters.LOCATION | filters.VENUE,
                    self._route_address_location_entry(profile_handlers.location_received),
                ),
                MessageHandler(
                    filters.TEXT & MenuTapFilter(
                        'telegram.address.enter_manually_button'
                    ),
                    self._consumes(profile_handlers.skip_location_sharing),
                ),
                MessageHandler(
                    filters.TEXT & cancel_tap,
                    self._consumes(profile_handlers.cancel_address_text),
                ),
            ],
            states={
                # Location sharing or manual entry choice. With
                # `allow_reentry=True`, PTB re-checks entry_points on every
                # update before falling back to this state's handlers, and the
                # entry-point LOCATION handler above matches unconditionally —
                # so this LOCATION registration never actually runs. Wrapped in
                # `_consumes` anyway (belt-and-suspenders): if that shadowing
                # ever stops holding, an unstopped pin here would reach the
                # group-0 attachment catch-all and get filed as a support
                # ticket right after being saved as an address.
                ADDRESS_LOCATION: [
                    MessageHandler(filters.LOCATION, self._consumes(profile_handlers.location_received)),
                    # "Enter manually" (initial choice) and "Re-enter address"
                    # (the retry keyboard) are one handler: two labels, the same
                    # destination. They were two identical registrations, which
                    # is one more place for the pair to drift apart.
                    MessageHandler(
                        filters.TEXT & address_manual_entry_tap,
                        self._consumes(profile_handlers.skip_location_sharing),
                    ),
                    # Handle "Cancel" text button from retry keyboard
                    MessageHandler(
                        filters.TEXT & cancel_tap,
                        self._consumes(profile_handlers.cancel_address_text),
                    ),
                ],
                # Address title input
                ADDRESS_TITLE: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.address_title_callback),
                        pattern="^addr_title_",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.address_title_received),
                    )
                ],
                # Manual entry flow - Region selection
                ADDRESS_REGION: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.region_selected),
                        pattern="^region_",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.cancel_address),
                        pattern="^cancel_address_creation$",
                    ),
                ],
                # Manual entry flow - District selection
                ADDRESS_DISTRICT: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.district_selected),
                        pattern="^district_",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.cancel_address),
                        pattern="^cancel_address_creation$",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.back_to_region),
                        pattern="^back_to_region$",
                    ),
                ],
                # Manual entry flow - Street input
                ADDRESS_STREET: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.skip_field_handler),
                        pattern="^skip_street$",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.street_received),
                    ),
                ],
                # Manual entry flow - Building input
                ADDRESS_BUILDING: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.skip_field_handler),
                        pattern="^skip_building$",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.building_received),
                    ),
                ],
                # Manual entry flow - Apartment input
                ADDRESS_APARTMENT: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.skip_field_handler),
                        pattern="^skip_apartment$",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.apartment_received),
                    ),
                ],
                # Manual entry flow - Floor input
                ADDRESS_FLOOR: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.skip_field_handler),
                        pattern="^skip_floor$",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.floor_received),
                    ),
                ],
                # Delivery instructions input
                ADDRESS_DELIVERY_INSTRUCTIONS: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.skip_field_handler),
                        pattern="^skip_delivery_instructions$",
                    ),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.delivery_instructions_received),
                    ),
                ],
                # Geocode confirmation
                ADDRESS_GEOCODE_CONFIRM: [
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.confirm_geocode),
                        pattern="^confirm_geocode$",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.retry_geocode),
                        pattern="^retry_geocode$",
                    ),
                    CallbackQueryHandler(
                        self._consumes(profile_handlers.cancel_address),
                        pattern="^cancel_address_creation$",
                    ),
                ],
                # `conversation_timeout` below is not self-announcing: PTB looks
                # for handlers under this key when the timer fires and, finding
                # none, ends the flow in total silence — leaving the customer on
                # a prompt whose buttons are dead and `address_flow_origin` /
                # `temp_address_data` stranded in user_data for the next flow to
                # trip over. `filters.ALL` because the synthetic timeout update
                # carries whatever the last real one was.
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, profile_handlers.address_flow_timeout),
                    CallbackQueryHandler(profile_handlers.address_flow_timeout),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", profile_handlers.cancel_address),
                CallbackQueryHandler(
                        self._consumes(profile_handlers.cancel_address),
                        pattern="^cancel_address_creation$",
                    ),
            ],
            per_chat=True,
            per_user=True,
            name="address_conversation",
            # 24h, not the 10 minutes this carried until 2026-08-26. Manual
            # entry is seven prompts long and customers routinely put the phone
            # down mid-way; expiring the same afternoon meant they came back to
            # dead buttons. Two consequences of the long window, both real:
            #
            # * While this flow is open every text the customer types is eaten
            #   by the step that asked for it (`_consumes` stops dispatch before
            #   the group-0 support catch-all), so it never reaches the Support
            #   Inbox. `/cancel`, the inline Cancel and every exit gesture
            #   (flow_exit.py) are the escapes.
            # * The Application has no `persistence`, so the state AND the
            #   APScheduler timeout job are in memory: a restart ends the flow
            #   silently, without this timeout's message. In practice the
            #   window is "24h or the next deploy, whichever comes first".
            #
            # NOT the same clock as `utils.AWAITING_LOCATION_STALE_MINUTES`.
            # That one governs a pin arriving with NO conversation open (the
            # zero-address checkout keyboard in handlers/orders.py, which arms
            # a pin prompt without entering this flow), so it is deliberately
            # shorter — it is what distinguishes a pin the bot asked for from a
            # spontaneous one bound for support. Inside THIS flow it is
            # bypassed: `temp_address_data` makes `mid_flow` true in
            # `_route_address_location_entry`.
            conversation_timeout=86400,  # 24 hours
            allow_reentry=True
        )
        # Add conversation handler in higher priority group
        self.application.add_handler(address_handler, group=-2)
        logger.info(f"Address conversation handler registered with states: {list(address_handler.states.keys())}")

        # Subscription creation conversation
        subscription_creation_timeout = self._flow_timeout(
            'telegram.subscription.flow_timed_out',
            *SUBSCRIPTION_CREATION_KEYS,
        )
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
                    # The quantity keyboard's Back button. Same destination as
                    # "add more items", different word on the button; it used
                    # to fall through to the group-0 ^back_to_product_ handler
                    # and die in int('selection').
                    CallbackQueryHandler(subscription_handlers.add_more_items, pattern="^back_to_product_selection$"),
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
                    # The payment keyboard's Back button, rendered by
                    # SubscriptionKeyboards.payment_methods since the flow was
                    # written and claimed by nothing until now.
                    CallbackQueryHandler(subscription_handlers.back_to_address_selection, pattern="^back_to_address_selection$"),
                    CallbackQueryHandler(subscription_handlers.cancel_subscription_creation, pattern="^cancel_subscription_creation$"),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, subscription_creation_timeout),
                    CallbackQueryHandler(subscription_creation_timeout),
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
        item_management_timeout = self._flow_timeout(
            'telegram.subscription.flow_timed_out',
            *ITEM_MANAGEMENT_KEYS,
        )
        item_management_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.add_item_start, pattern="^add_item_")],
            states={
                subscription_handlers.ITEM_SELECT_PRODUCT: [
                    CallbackQueryHandler(subscription_handlers.add_item_select_quantity, pattern="^sub_product_"),
                ],
                subscription_handlers.ITEM_SELECT_QUANTITY: [
                    # `update_item_confirm` used to be listed here too, behind
                    # the IDENTICAL pattern, so PTB could never reach it. It
                    # was not merely dead, it was the wrong flow: this
                    # conversation is entered only through `^add_item_`, its
                    # quantity step is reached from `add_item_select_quantity`
                    # (which sets `adding_product_id`), and
                    # `update_item_confirm` needs `editing_item_id` — which
                    # only the separate `update_item` conversation ever sets.
                    CallbackQueryHandler(subscription_handlers.add_item_confirm, pattern="^sub_qty_"),
                    CallbackQueryHandler(subscription_handlers.add_item_back_to_products, pattern="^back_to_product_selection$"),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, item_management_timeout),
                    CallbackQueryHandler(item_management_timeout),
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

        # The two follow-up steps of the "edit an existing subscription" branch.
        #
        # These used to be two ConversationHandlers built right here and never
        # passed to `add_handler` — the "# handled inline" comment beneath each
        # was simply false, and nothing in any group claimed `^subscription_freq_`
        # or `^sub_payment_` outside the CREATION conversation. Changing a
        # subscription's frequency or payment rail from the bot had never once
        # worked: the keyboard rendered, the tap matched nothing, the button
        # spun to Telegram's client timeout.
        #
        # Registered TOP LEVEL rather than rebuilt as conversations, in their own
        # `subfreq_` / `subpay_` namespaces (keyboards.py). Both properties are
        # load-bearing:
        #   * top level + id on the callback = the screen still works after the
        #     deploy that empties `user_data`, and two open cards can never edit
        #     each other's subscription.
        #   * a separate namespace = no competition with the creation flow, which
        #     legitimately owns `^subscription_freq_` and `^sub_payment_` in its
        #     SELECT_ADDRESS / CONFIRM_SUBSCRIPTION states.
        # tests/telegram_bot/test_subscription_edit_journeys.py
        self.application.add_handler(CallbackQueryHandler(
            subscription_handlers.update_frequency_confirm,
            pattern=r"^subfreq_[a-z]+_\d+$",
        ))
        self.application.add_handler(CallbackQueryHandler(
            subscription_handlers.change_payment_method_confirm,
            pattern=r"^subpay_[a-z_]+_\d+$",
        ))

        # Item quantity update conversation
        update_item_timeout = self._flow_timeout(
            'telegram.subscription.flow_timed_out',
            *UPDATE_ITEM_KEYS,
        )
        update_item_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(subscription_handlers.update_item_quantity, pattern="^update_item_")],
            states={
                subscription_handlers.ITEM_SELECT_QUANTITY: [
                    CallbackQueryHandler(subscription_handlers.update_item_confirm, pattern="^sub_qty_"),
                ],
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, update_item_timeout),
                    CallbackQueryHandler(update_item_timeout),
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

        # Message handlers (catch-all). Deliberately still in the DEFAULT
        # group, BEHIND the conversations: free text with no flow open is a
        # support message and must reach `_capture_support_message`, while text
        # a conversation asked for never gets here because that step's callback
        # is wrapped in `_consumes` and raises ApplicationHandlerStop.
        #
        # `~filters.UpdateType.EDITED_MESSAGE` is load-bearing, not decoration.
        # `filters.TEXT` MATCHES an edit — PTB tests `update.effective_message`,
        # which resolves to `edited_message` — while `update.message` is None on
        # one, so `_handle_text_message` raised AttributeError and its own except
        # branch raised a second one from `update.message.reply_text(...)`,
        # escaping to the global handler with the customer shown nothing. A
        # customer fixing a typo was enough; no restart or open flow needed.
        #
        # Excluded rather than accommodated: an edit is not new input. Re-running
        # this catch-all over one would file a second Support Inbox row for a
        # message already captured, and re-consume an OTP already spent.
        # tests/telegram_bot/test_edited_message_router.py
        #
        # The filter is `flow_exit.FREE_TEXT_FILTER`, the SAME object the flow
        # exit matches labels with: this router no longer resolves labels
        # itself, so a text this handler gets must be one the exit could see.
        self.application.add_handler(
            MessageHandler(FREE_TEXT_FILTER, self._handle_text_message)
        )

        # Contact messages are handled by ConversationHandlers in higher priority groups
        # (registration_handler and phone_verification_handler)

        # Location (and venue) messages are routed entirely by the address
        # conversation's entry point in group -2 — see
        # `_route_address_location_entry` — which decides per-update whether
        # a pin is address-bound or a spontaneous support message. They are
        # deliberately NOT in the filter set below: this handler would only
        # ever see a location/venue update AFTER that entry point already
        # claimed and stopped it, so including them here would be dead code
        # that could only double-handle.

        # A shared CONTACT with no conversation open.
        #
        # `filters.CONTACT` is otherwise registered ONLY inside two conversation
        # states (registration PHONE, phone-verification PHONE_VERIFY_PHONE),
        # both in group -2. But the keyboard that asks for it is a
        # `ReplyKeyboardMarkup` with `request_contact=True` — CLIENT-side state
        # that survives every restart — so after a deploy the contact arrived
        # and matched nothing in any group. Nothing ran, nothing was said, and
        # `one_time_keyboard=True` then took the keyboard away: no message, no
        # error, and no way to finish signing up short of knowing to re-send
        # /start.
        #
        # DEFAULT group, so the two conversations still claim it first whenever
        # one is genuinely open (they are in group -2 and stop propagation).
        # This only ever sees the contact nobody else wanted.
        # tests/telegram_bot/test_signup_journey_after_restart.py
        self.application.add_handler(
            MessageHandler(filters.CONTACT, self._handle_contact)
        )

        # See ADDRESS_FLOW_BUTTONS above.
        self.application.add_handler(
            CallbackQueryHandler(
                self._address_form_expired, pattern=self.ADDRESS_FLOW_BUTTONS
            )
        )

        # The account-link answers, for the same reason and in the same shape.
        self.application.add_handler(
            CallbackQueryHandler(
                self._signup_step_expired, pattern=r"^(link_yes|link_no)$"
            )
        )

        # The web-link confirm card's buttons, for the same reason: after a
        # restart, the timeout or a plain /start the token is gone, and the tap
        # says the link expired (spec 2026-10-19 D15c).
        self.application.add_handler(
            CallbackQueryHandler(
                profile_handlers.web_link_step_expired, pattern=r"^weblink_(yes|no)$"
            )
        )

        # Other attachments with no flow open are support messages. DEFAULT
        # group and registered AFTER the text catch-all on purpose:
        #   * the address and registration conversations own filters.CONTACT
        #     in group -2 and stop propagation;
        #   * within one group only the first matching handler runs.
        # Sticker/Animation/Poll/Dice have no dedicated `message_type` — they
        # fall through `build_support_payload` to UNSUPPORTED (type label
        # only, no payload) so a customer's sticker is recorded rather than
        # silently dropped, matching Goal 4. See
        # tests/telegram_bot/test_support_attachment_dispatch.py.
        self.application.add_handler(
            MessageHandler(
                filters.PHOTO
                | filters.Document.ALL
                | filters.VIDEO
                | filters.VIDEO_NOTE
                | filters.AUDIO
                | filters.VOICE
                | filters.Sticker.ALL
                | filters.ANIMATION
                | filters.POLL
                | filters.Dice.ALL,
                self._handle_attachment_message,
            )
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

            # A bottom-keyboard label is navigation, whatever else is armed:
            # it must never reach typed quantity, OTP, awaiting_input, the
            # address draft or the Support Inbox capture below. The flow exit
            # (group -3) already resolved it and left the open flows.
            menu_key = pending_menu_tap(update, context)
            if menu_key is not None:
                await self._dispatch_menu_tap(menu_key, update, context)
                return

            # The durable half is read FIRST, because the in-memory half does
            # not survive the deploy this branch exists to survive.
            user_state = await self.user_repository.get_user_state(user_id)

            # A number typed at the cart quantity screen. Checked BEFORE every
            # armed prompt on purpose: anything else the customer does closes
            # the screen's typing window (handlers/quantity_screen.py), so a
            # live window means the quantity screen is the NEWEST thing they
            # touched, and a number typed under it is meant for it. Text that
            # does not read as a quantity — including a 6-digit verification
            # code, which never does — is declined untouched and falls through
            # to the routing below.
            # tests/telegram_bot/test_typed_quantity_journeys.py
            if await product_handlers.handle_typed_quantity(update, context, text, user_state, language):
                return

            # Awaiting the phone-verification code? `awaiting_otp` is the live
            # marker; `bot_state.awaiting_input == 'phone_otp'` is the same fact
            # written somewhere a restart cannot reach. Either one means the next
            # 6 digits are an OTP and NOT a support message — which is what they
            # silently became, unredacted, whenever a deploy landed while the SMS
            # was in flight.
            # tests/telegram_bot/test_armed_prompts_survive_a_restart.py
            #
            # Spec 2026-10-21 §2, "Phone-code steps": a code typed within
            # PHONE_OTP_EXPIRY of its SMS verifies, even after the customer
            # tapped elsewhere or typed something else first (an exit never
            # disarms `phone_otp`, and neither does other text). Text that is
            # not a code is routed below like any other message; once the
            # lifetime is over, the prompt is disarmed.
            # tests/telegram_bot/test_phone_code_steps.py
            if context.user_data.get('awaiting_otp') or user_state.get('awaiting_input') == 'phone_otp':
                prompted_update_id = context.user_data.get('otp_prompted_update_id')
                if prompted_update_id == update.update_id:
                    # OTP was just prompted from this same update (e.g. phone text).
                    # Skip immediate re-processing of the same text as OTP.
                    context.user_data.pop('otp_prompted_update_id', None)
                    return
                if profile_handlers.may_arm_phone_otp(user_state) and not user_state.get(PHONE_OTP_ARMED_AT):
                    # Armed before this release, or the durable arm write failed:
                    # stamp it now, so a live code still verifies and never lands
                    # in the Support Inbox. Such a prompt lives at most one more
                    # PHONE_OTP_EXPIRY. A NEWER prompt holding `awaiting_input` is
                    # never overwritten: its text is its own.
                    await profile_handlers.arm_phone_otp(
                        context, user_id,
                        user_state.get('pending_phone_verification')
                        or context.user_data.get('pending_phone_verification'),
                    )
                    user_state = await self.user_repository.get_user_state(user_id)
                if profile_handlers.phone_code_expired(user_state):
                    # The code's lifetime is over, or a newer prompt holds
                    # `awaiting_input`: the code step is gone, and this text is
                    # routed like any other.
                    await profile_handlers.disarm_phone_otp(context, user_id)
                    user_state = await self.user_repository.get_user_state(user_id)
                else:
                    code = otp_code_in(text)
                    if code is not None:
                        # Rehydrate what the dead process was holding, so the verifier
                        # knows which number this code belongs to.
                        if not context.user_data.get('pending_phone_verification'):
                            pending = user_state.get('pending_phone_verification')
                            if pending:
                                context.user_data['pending_phone_verification'] = pending
                        context.user_data['awaiting_otp'] = True
                        await self._handle_otp_verification(update, context, code, language)
                        return
                    # Not a code: routed below like any other message, with no
                    # "invalid code" reply. The prompt stays armed, so the SMS that
                    # arrives afterwards still verifies (spec §2, "Why").

            # A live `phone_otp` prompt is answered by the branch above and by
            # nothing else: the non-code text it let through goes on to support.
            if user_state.get('awaiting_input') not in (None, 'phone_otp'):
                # Handle contextual input
                await self._handle_contextual_input(update, context, user_state, language)
            elif user_state.get('address_draft'):
                # They are ANSWERING the address form, not writing to support.
                #
                # Manual entry is seven typed prompts and the conversation that
                # asks them dies with the process, so after a deploy the customer
                # is left on "Enter the street" with nothing listening. Their
                # street name, building number and delivery instructions were
                # then filed one by one into the admin Support Inbox as
                # unsolicited messages — silently, so nothing on screen changed
                # and they kept typing.
                #
                # `address_draft` is the durable evidence they were mid-form
                # (every step dual-writes it to `users.bot_state`, SDD
                # 2026-08-26-address-flow-bot-state). Cleared as we answer, so
                # the next thing they send is an ordinary support message rather
                # than this reply again.
                # tests/telegram_bot/test_address_text_after_restart.py
                await update.message.reply_text(
                    i18n.get('telegram.address.flow_timed_out', language)
                )
                try:
                    await self.user_repository.clear_address_draft(user_id)
                except Exception as clear_error:
                    logger.warning(
                        "Could not clear the stranded address draft for %s: %s",
                        user_id, clear_error,
                    )
            else:
                # General free text with no active flow (not OTP, not a conversation
                # state): silently capture it as a support message so an admin can
                # reply from the admin UI. No auto-acknowledgement is sent.
                await self._capture_support_message(update, context, text)

        except Exception as e:
            logger.error(f"Error handling text message: {e}")
            await update.message.reply_text(i18n.get('telegram.error_occurred', language))

    async def _capture_support_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       text: str = None):
        """Silently persist an unsolicited message so an admin can reply from the
        admin UI. Extraction and posting live in `support_capture` — see that
        module's docstring for why."""
        await capture_support_message(update, context)

    async def _capture_support_with_guards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Guard sequence shared by every path that silently files an
        unsolicited update to the support inbox: rate limit -> user_middleware
        -> the armed "Report an issue" concern-flow check -> capture.

        Used by `_handle_attachment_message` (photo/document/video/audio/
        voice) AND `_route_address_location_entry`'s spontaneous-pin branch.
        Before this was extracted, the pin branch called
        `capture_support_message` directly and skipped every one of these —
        with "Report an issue" armed, a photo correctly acked under the
        `[Order #N]` prefix and cleared the concern state, but a pin was filed
        bare, unacked, left the concern armed, and the customer's NEXT
        unrelated message was then wrongly acked under that order number. Pins
        also got no rate limiting at all. One implementation, two call sites.
        """
        user_id = update.effective_user.id

        if not await rate_limiter.allow_request(user_id):
            language = await i18n.get_user_language(user_id)
            await update.message.reply_text(
                i18n.get('telegram.bot.rate_limit_exceeded', language)
            )
            return

        user = await user_middleware(update)
        if not user:
            return

        user_state = await self.user_repository.get_user_state(user_id)
        if user_state.get('awaiting_input') == 'support_message':
            # The armed "Report an issue" concern flow: same prefix, same ack.
            await support_flow_handlers.handle_support_attachment(update, context)
            return

        await capture_support_message(update, context)

    async def _handle_attachment_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Capture a photo/document/voice/video sent with no flow open.

        Location and venue pins are NOT handled here — see
        `_route_address_location_entry` — because a pin's disposition depends
        on whether the bot asked for it, which only the address conversation's
        entry point can decide. Both paths share the same guard sequence via
        `_capture_support_with_guards`.

        Mirrors `_handle_text_message`'s guards exactly. An attachment arriving
        while some OTHER flow awaits text is still support: that flow did not
        ask for a photo.
        """
        try:
            await self._capture_support_with_guards(update, context)
        except Exception as e:
            logger.error(f"Error handling attachment message: {e}")

    async def _dispatch_menu_tap(self, key: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open the screen a bottom-keyboard label stands for (the one label -> screen table).

        A key with no row raises KeyError, which `_handle_text_message` logs; it
        never falls through to a silent support capture.
        """
        async def open_cart(update, context):
            context.user_data.pop('cart_edit_return', None)
            await product_handlers.show_cart(update, context, edit_mode=False)

        table = {
            "telegram.menu.products": quick_order_handlers.order_water,
            "telegram.menu.orders": order_handlers.orders_menu,
            "telegram.cart_title": open_cart,
            "telegram.menu.subscriptions": subscription_handlers.subscriptions_menu,
            "telegram.menu.loyalty": loyalty_handlers.loyalty_menu,
            "telegram.menu.profile": profile_handlers.profile_menu,
            "telegram.menu.support": support_handlers.support_menu,
        }
        await table[key](update, context)

    async def _handle_otp_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       code: str, language: str):
        """Handle OTP verification from user input"""
        try:
            user_id = update.effective_user.id

            # Verify OTP via API. `code` is the router's `otp_code_in`, never raw text.
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await update.message.reply_text(
                        i18n.get('telegram.bot.otp.auth_error', language)
                    )
                    await profile_handlers.disarm_phone_otp(context, user_id)
                    return

                response = await client.verify_phone_otp(user_token, code)
                if response.success:
                    await send_menu_keyboard(
                        context.bot, update.effective_chat.id, user_id,
                        i18n.get('telegram.bot.otp.success_message', language), language,
                        parse_mode='Markdown',
                    )

                    # Clear OTP flags
                    await profile_handlers.disarm_phone_otp(context, user_id)

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
            await profile_handlers.disarm_phone_otp(context, user_id)

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
            # Guided concern capture armed by the delivered-summary Report button.
            await support_flow_handlers.handle_support_message(update, context, text)
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
            # Unknown state, clear it. `input_type` is `user_state['awaiting_input']`,
            # read immediately before this dispatch (`_handle_text_message`), so
            # it names exactly the flow(s) armed here.
            await self.user_repository.disarm(user_id, input_type)
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

    # Every button the seven address-entry screens can render, claimed at TOP
    # LEVEL so it still lands once the conversation that owned it is gone.
    #
    # `conversation_timeout` is 86400s and manual entry is seven prompts, so a
    # customer parked mid-form across a deploy is the ordinary case. The state
    # map dies with the process; the inline keyboard does not. Every one of
    # these used to match nothing in any group afterwards — no handler ran,
    # nothing answered the callback query, and Telegram spun the button to its
    # own client timeout and gave up in silence. Cancel was the cruellest: the
    # one button whose entire job is "get me out of here" was the one button
    # that could not.
    #
    # Plain `(a|b|c)` alternation and `\w+` rather than non-capturing groups,
    # so `test_callback_contract_customer`'s collision sampler can still read
    # it — an unreadable pattern makes that whole check go blind one handler at
    # a time.
    #
    # Safe to sit in group 0 beside the live conversation in group -2 because
    # those state handlers are wrapped in `_consumes`: when the flow really IS
    # open it claims the tap and stops dispatch before this ever sees it.
    # tests/telegram_bot/test_address_buttons_after_restart.py
    ADDRESS_FLOW_BUTTONS = (
        r"^(cancel_address_creation|back_to_region|confirm_geocode|retry_geocode"
        r"|skip_street|skip_building|skip_apartment|skip_floor"
        r"|skip_delivery_instructions|region_\w+|district_\w+|addr_title_\w+)$"
    )

    async def _signup_step_expired(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Answer a registration button whose conversation is gone.

        `link_yes` / `link_no` are the two answers to the account-link prompt,
        which appears at the most delicate moment in signup: the phone the
        customer just shared already belongs to a web account. Both were
        registered ONLY inside the registration conversation's
        LINK_ACCOUNT_CONFIRM state, so after a deploy neither answer landed
        anywhere — the customer is left mid-signup, in front of a question about
        their own account, and both buttons do nothing at all.

        Reuses the seeded `telegram.registration.flow_timed_out`, and points
        them at /start, which is the only thing that can restart signup.
        tests/telegram_bot/test_menu_and_link_buttons_after_restart.py
        """
        query = update.callback_query
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            await query.answer(
                i18n.get('telegram.registration.flow_timed_out', language),
                show_alert=True,
            )
            if query.message is not None:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning("Could not answer an expired signup button: %s", e)

    async def _address_form_expired(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Answer a button from an address form whose conversation is gone.

        Reuses the already-seeded `telegram.address.flow_timed_out` — "the
        address form timed out and was closed. Nothing was saved — you can start
        again any time" — which is exactly what happened, and means this fix
        ships without a translation seed.
        """
        query = update.callback_query
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            text = i18n.get('telegram.address.flow_timed_out', language)
            await query.answer(text, show_alert=True)
            if query.message is not None:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning("Could not answer an expired address button: %s", e)

    async def _handle_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle contact sharing"""
        try:
            user_id = update.effective_user.id
            contact = update.message.contact

            if contact.user_id == user_id:
                # IDEMPOTENT, because this now runs as a group-0 FALLBACK for a
                # contact no conversation claimed — and a reply-keyboard tap has
                # no dedup middleware behind it (that guards inline buttons
                # only). Somebody on a slow connection taps Share again after
                # the flow has already finished; without this the second tap
                # re-ran the whole tail of registration and re-asked for a name
                # the customer had already given.
                # tests/telegram_bot/test_registration_journey_dispatcher.py
                existing = await self.user_repository.get_user_by_telegram_id(user_id)
                if existing and existing.get('phone'):
                    logger.info(
                        "Ignoring a duplicate shared contact for %s; the phone "
                        "is already recorded.", user_id,
                    )
                    return

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

            # Close the shared backend HTTP client. This is the ONLY place it
            # is closed — handlers' `async with api_client` no longer closes
            # it, because the client is process-wide (see api_client.start()).
            logger.info("Closing API client...")
            await api_client.aclose()

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
