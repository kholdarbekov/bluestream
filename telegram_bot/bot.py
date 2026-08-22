"""
Main Telegram Bot Application
Comprehensive water business bot with full feature integration
"""
import asyncio
import functools
import logging
import re
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
    Application, ApplicationHandlerStop, CommandHandler, MessageHandler,
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
from webhook_server import webhook_server
from token_manager import TokenManager
from handlers import (
    main_menu_handler, language_handler,
    product_handlers, order_handlers, subscription_handlers,
    profile_handlers, loyalty_handlers, admin_handlers,
    support_handlers, support_flow_handlers, payment_handlers, bottle_handlers,
    quick_order_handlers,
)
# Import conversation states directly (they are module-level constants)
from handlers.profile import (
    SELECT_LANGUAGE, PHONE, LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP,
    ADDRESS_LOCATION, ADDRESS_TITLE, ADDRESS_REGION, ADDRESS_DISTRICT,
    ADDRESS_STREET, ADDRESS_BUILDING, ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM
)
from eligibility import main_menu_for
from utils import error_handler, rate_limiter, user_middleware, get_auth_token
from keyboards import MenuKeyboards, PRODUCT_PAGE_PATTERN

logger = logging.getLogger('bot')


# ---------------------------------------------------------------------------
# "Is this text a reply-keyboard tap?" -- ONE rule, one place
# ---------------------------------------------------------------------------
# The address flow arms REPLY keyboards (`ProfileKeyboards.location_request`
# with `extra_rows=`), so Enter-manually / Re-enter / Cancel arrive as ordinary
# text. Unlike the staff bot, the KEYBOARD adds no decoration of its own here:
# the row is rendered verbatim from `i18n.get`, and any emoji is part of the
# seeded copy ("❌ Bekor qilish"). The decoration therefore sits on EITHER side
# of the comparison -- a keyboard still on the customer's phone was rendered
# from the row as it read then, which may carry an emoji the row has since lost
# (or vice versa) -- so the strip is symmetric.
#
# It is an EMOJI strip, and this regex is the only place that says so. What it
# replaces was `(?:\S+\s+)?`, "any single leading token", which is the shape
# wave 3 deleted from the staff bot: there it let a five-character first word
# ("Sardor Profil") satisfy the FILTER while the matcher resolved nothing, and
# the conversation was torn down with no output at all. Here the same shape
# meant "word Bekor qilish" cancelled the address flow. Ranges rather than a
# library: `re` has no \p{Emoji}, and "strip any leading non-alphanumeric run"
# would quietly re-admit "+998...".
_EMOJI_PREFIX_RE = re.compile(
    r"^(?:"
    r"[\U0001F000-\U0001FAFF]"       # pictographs, transport, flags, extended-A
    r"|[\u2190-\u21FF]"              # arrows
    r"|[\u2300-\u23FF]"              # misc technical (watch, hourglass)
    r"|[\u2460-\u27BF]"              # enclosed alphanumerics .. dingbats (gear, check)
    r"|[\u2B00-\u2BFF]"              # misc symbols and arrows (left arrow, star)
    r"|[\u3030\u303D\u3297\u3299]"   # wavy dash, part alternation mark, congrat/secret
    r"|[\uFE00-\uFE0F\u200D\u20E3]"  # variation selectors, ZWJ, combining keycap
    r")+"
)


def _bare_label(text: Optional[str]) -> str:
    """`text` stripped of surrounding space and of a leading emoji decoration."""
    return _EMOJI_PREFIX_RE.sub("", (text or "").strip()).strip()


def _is_tap_on_label(text: Optional[str], label: Optional[str]) -> bool:
    """Is `text` a tap on a reply-keyboard button reading `label`?

    Whole-string, never a substring: the pattern this replaces once matched any
    message CONTAINING the copy, so "I don't want to cancel" cancelled the
    address flow -- the bot agreeing with the customer by ending their work.
    Text that merely contains a label is not a tap and is left to the state's
    own handler, which is what a customer typing a sentence is owed.

    Equal after the emoji strip, so the decoration may differ between the
    keyboard on the phone and the row as it reads now -- but only the
    decoration. A pure-emoji label strips to nothing and is compared raw, so
    two different emoji buttons can never collapse into each other.
    """
    text = (text or "").strip()
    label = (label or "").strip()
    if not text or not label:
        return False
    if text == label:
        return True
    bare_text, bare_label = _bare_label(text), _bare_label(label)
    return bool(bare_text) and bare_text == bare_label


def _resolve_tapped_label(text: Optional[str], translation_keys) -> Optional[str]:
    """Which of `translation_keys` is this text a tap on? The key, or None.

    THE decider, and the only copy of the rule. `MenuTapFilter` -- the only
    thing that asks -- calls this and nothing else, so "the filter claimed it"
    and "the matcher resolved it" cannot become different answers.

    Resolved WHEN THE TAP ARRIVES: `i18n.get` is called here, exactly as the
    keyboard builder calls it when it renders. Nothing is memoised across
    updates, and that is the feature -- an admin retitling a label in the admin
    UI (or any `i18n.reload_translations()`) changes what the button says AND
    what answers it in the same instant, with no restart, and the retired copy
    stops matching at the same instant so it cannot linger as a hotword that
    hijacks typed text. A matcher frozen at handler-build time left the button
    the customer could SEE dead, and an unmatched tap in ADDRESS_LOCATION
    escapes to the group-0 catch-all, where Cancel becomes a support ticket
    with no reply.

    Swept across EVERY supported language, not just the customer's: a reply
    keyboard is client-side and survives a language switch until Telegram
    redraws it, so a tap can arrive in the language the customer just left.
    Key order breaks a tie if two keys ever render the same copy.

    Cost per text update: one `i18n.get` -- an in-memory dict lookup plus
    `render_translation` on a placeholder-free label -- per key per language,
    abandoned on the first hit. The wiring registers at most three keys against
    one update (Enter-manually + Re-enter, then Cancel) over three languages,
    so under 10 lookups, a few microseconds. The one cost that is not free is
    an UNSEEDED label key: `i18n.get` logs a missing-key warning per language
    per update instead of once at startup. It still resolves -- both sides
    derive the same `humanised_missing_key` -- it is just loud, which is the
    correct volume for a reply-keyboard row that was never seeded.
    """
    for translation_key in translation_keys:
        for lang_code in i18n.supported_languages:
            if _is_tap_on_label(text, i18n.get(translation_key, lang_code)):
                return translation_key
    return None


class MenuTapFilter(filters.MessageFilter):
    """``True`` for text that IS a tap on one of `translation_keys`.

    A `filters.MessageFilter` rather than a `filters.Regex` because a regex can
    only be built from copy that is already known, i.e. at handler-build time;
    this asks `_resolve_tapped_label` per update instead. Registered in place
    of the build-time label regexes that guarded the customer's way OUT of the
    address flow.
    """

    __slots__ = ("_translation_keys",)

    def __init__(self, *translation_keys: str):
        super().__init__(name="telegram_menu_tap:%s" % ",".join(translation_keys))
        self._translation_keys = translation_keys

    def filter(self, message) -> bool:
        return _resolve_tapped_label(message.text, self._translation_keys) is not None


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
        (CONTACT, LOCATION, and the callback patterns that appear only inside a
        conversation), because stopping dispatch there would buy nothing and
        would silence the group -1 callback logger for no reason.
        """
        @functools.wraps(callback)
        async def _stop_after_this_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
            raise ApplicationHandlerStop(await callback(update, context))

        return _stop_after_this_conversation

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
                # A half-registered customer has no menu to go back to, and the
                # step they abandoned left a request_contact REPLY keyboard on
                # their screen; clearing it is the only honest thing to show.
                keyboard = (
                    await main_menu_for(user_id, language) if offer_menu
                    else ReplyKeyboardRemove()
                )

                # The timeout update is synthetic — it carries whatever the
                # customer's last real one was — so the reply target is
                # derived rather than assumed.
                query = update.callback_query
                if query is not None and query.message is not None:
                    await query.message.reply_text(text, reply_markup=keyboard)
                elif update.message is not None:
                    await update.message.reply_text(text, reply_markup=keyboard)
                else:
                    await context.bot.send_message(
                        chat_id=user_id, text=text, reply_markup=keyboard
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
            # against it with stateful handlers and this bot's checkout is a
            # ConversationHandler. PerChatSerialUpdateProcessor keeps each
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

            # Order callbacks
            CallbackQueryHandler(order_handlers.orders_menu, pattern="^menu_orders$"),
            CallbackQueryHandler(order_handlers.order_details, pattern="^order_"),
            # Reward-in-checkout handlers MUST precede the broad "^checkout" catch-all
            # below, or it would swallow these checkout_* callbacks.
            CallbackQueryHandler(order_handlers.checkout_choose_reward, pattern="^checkout_choose_reward$"),
            CallbackQueryHandler(order_handlers.checkout_apply_reward, pattern="^checkout_apply_reward_\\d+$"),
            CallbackQueryHandler(order_handlers.checkout_remove_reward, pattern="^checkout_remove_reward$"),
            CallbackQueryHandler(order_handlers.checkout_change_address, pattern="^checkout_change_address$"),
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
            'pending_phone', 'pending_phone_verification', 'pending_link_phone',
            'awaiting_otp', 'otp_prompted_update_id',
            offer_menu=False,
        )
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
                    MessageHandler(filters.CONTACT, profile_handlers.phone_received),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.phone_text_received),
                    )
                ],
                # Account linking states
                LINK_ACCOUNT_CONFIRM: [
                    CallbackQueryHandler(profile_handlers.link_account_confirm, pattern="^link_")
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
                # 300 seconds is well inside normal Uzbek SMS latency, so this
                # is the flow the silent timeout hurt most. `awaiting_otp` and
                # friends MUST go with it: they are read by the group-0 text
                # catch-all, which would otherwise send the next thing this
                # customer types to the phone-verification endpoint. No main
                # menu — they are not registered yet — and ReplyKeyboardRemove
                # clears the request_contact keyboard the phone step left up.
                ConversationHandler.TIMEOUT: [
                    MessageHandler(filters.ALL, registration_timeout),
                    CallbackQueryHandler(registration_timeout),
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
            entry_points=[
                CallbackQueryHandler(profile_handlers.add_address, pattern="^add_new_address(_checkout)?$"),
                # A pin (or a manual/cancel tap) can arrive BEFORE the flow has
                # started, because zero-address checkout arms the keyboard
                # itself. Without these the tap escapes to the group-0 catch-all
                # and is silently filed as a support ticket.
                MessageHandler(filters.LOCATION, profile_handlers.location_received),
                MessageHandler(
                    filters.TEXT & MenuTapFilter(
                        'telegram.address.enter_manually_button'
                    ),
                    self._consumes(profile_handlers.skip_location_sharing),
                ),
                MessageHandler(
                    filters.TEXT & MenuTapFilter('telegram.cancel'),
                    self._consumes(profile_handlers.cancel_address_text),
                ),
            ],
            states={
                # Location sharing or manual entry choice
                ADDRESS_LOCATION: [
                    MessageHandler(filters.LOCATION, profile_handlers.location_received),
                    # "Enter manually" (initial choice) and "Re-enter address"
                    # (the retry keyboard) are one handler: two labels, the same
                    # destination. They were two identical registrations, which
                    # is one more place for the pair to drift apart.
                    MessageHandler(
                        filters.TEXT & MenuTapFilter(
                            'telegram.address.enter_manually_button',
                            'telegram.address.reenter_manually_button',
                        ),
                        self._consumes(profile_handlers.skip_location_sharing),
                    ),
                    # Handle "Cancel" text button from retry keyboard
                    MessageHandler(
                        filters.TEXT & MenuTapFilter('telegram.cancel'),
                        self._consumes(profile_handlers.cancel_address_text),
                    ),
                ],
                # Address title input
                ADDRESS_TITLE: [
                    CallbackQueryHandler(profile_handlers.address_title_callback, pattern="^addr_title_"),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.address_title_received),
                    )
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
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.street_received),
                    ),
                ],
                # Manual entry flow - Building input
                ADDRESS_BUILDING: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_building$"),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.building_received),
                    ),
                ],
                # Manual entry flow - Apartment input
                ADDRESS_APARTMENT: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_apartment$"),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.apartment_received),
                    ),
                ],
                # Manual entry flow - Floor input
                ADDRESS_FLOOR: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_floor$"),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.floor_received),
                    ),
                ],
                # Delivery instructions input
                ADDRESS_DELIVERY_INSTRUCTIONS: [
                    CallbackQueryHandler(profile_handlers.skip_field_handler, pattern="^skip_delivery_instructions$"),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.delivery_instructions_received),
                    ),
                ],
                # Geocode confirmation
                ADDRESS_GEOCODE_CONFIRM: [
                    CallbackQueryHandler(profile_handlers.confirm_geocode, pattern="^confirm_geocode$"),
                    CallbackQueryHandler(profile_handlers.retry_geocode, pattern="^retry_geocode$"),
                    CallbackQueryHandler(profile_handlers.cancel_address, pattern="^cancel_address_creation$"),
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
        phone_verification_timeout = self._flow_timeout(
            'telegram.phone.verification_flow_timed_out',
            'pending_phone',
        )
        phone_verification_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(profile_handlers.add_phone_number, pattern="^add_phone_number$"),
            ],
            states={
                profile_handlers.PHONE_VERIFY_PHONE: [
                    MessageHandler(filters.CONTACT, profile_handlers.phone_verify_contact_received),
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
                        self._consumes(profile_handlers.phone_verify_text_received),
                    ),
                ],
                profile_handlers.PHONE_VERIFY_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND,
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

        # Subscription creation conversation
        subscription_creation_timeout = self._flow_timeout(
            'telegram.subscription.flow_timed_out',
            'subscription_creation', 'current_product_id',
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
            'editing_subscription_id', 'adding_product_id',
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
        update_item_timeout = self._flow_timeout(
            'telegram.subscription.flow_timed_out',
            'editing_subscription_id', 'editing_item_id',
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
