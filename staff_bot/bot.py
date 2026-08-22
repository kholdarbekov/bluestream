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
import time
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

from telegram import Update, BotCommand, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes, TypeHandler
)
from telegram.error import NetworkError, TimedOut
from shared.telegram_request import ResilientHTTPXRequest
from shared.telegram_update_processor import PerChatSerialUpdateProcessor

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
from staff_bot.keyboards.menu import MenuKeyboards
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
from staff_bot.handlers.operator.redispatch import RedispatchHandler
from staff_bot.handlers.common.profile import ProfileHandler
from staff_bot.handlers.common.help import HelpHandler
from staff_bot.permissions import require_auth

logger = logging.getLogger('staff_bot')


# ---------------------------------------------------------------------------
# "Is this text a reply-keyboard tap?" -- ONE rule, one place
# ---------------------------------------------------------------------------
# The staff menu is a REPLY keyboard, so every tap arrives as ordinary text
# carrying a decoration the KEYBOARD added: `MenuKeyboards.main_menu` renders
# f"<emoji> {label}". Recognising a tap therefore means removing that
# decoration and comparing what is left against the translated label.
#
# The decoration is an EMOJI, and this regex is the only place that says so.
# It used to be said twice and differently -- "any 2-4 CHARACTERS" inside
# `_match_menu_action`, "any single \S+ token" inside the escape regex -- and
# the two answers disagreed in both directions:
#
#   * "Aziz Profil" (4 chars + space + the Uzbek Profile label) was read as a
#     Profile tap and navigated an operator out of the client they were
#     half-way through creating;
#   * "Sardor Profil" (6 chars) was claimed by the escape FILTER and resolved
#     by the MATCHER to nothing, so the conversation was torn down and NOBODY
#     answered -- the operator watched the bot go silent mid-flow.
#
# Ranges rather than a library: `re` has no \p{Emoji}, and the alternative
# (strip any leading non-alphanumeric run) would quietly re-admit "+998...".
_EMOJI_PREFIX_RE = re.compile(
    r"^(?:"
    r"[\U0001F000-\U0001FAFF]"       # pictographs, transport, flags, extended-A
    r"|[\u2190-\u21FF]"              # arrows
    r"|[\u2300-\u23FF]"              # misc technical (watch, hourglass)
    r"|[\u2460-\u27BF]"              # enclosed alphanumerics .. dingbats (gear, question, check)
    r"|[\u2B00-\u2BFF]"              # misc symbols and arrows (left arrow, star)
    r"|[\u3030\u303D\u3297\u3299]"     # wavy dash, part alternation mark, congrat/secret
    r"|[\uFE00-\uFE0F\u200D\u20E3]"    # variation selectors, ZWJ, combining keycap
    r")+"
)


def _menu_tap_candidates(text: Optional[str]) -> list:
    """The label(s) a reply-keyboard tap could be carrying.

    The text as sent (a staff member who retyped the label by hand, or a
    keyboard rendered before the emoji changed) and, when the text opens with
    an emoji the keyboard could have added, the text with that emoji removed.

    Nothing else. Anything a person typed in front of a label -- a name, a
    word, a note -- leaves the text a non-label, which is the whole point.
    """
    text = (text or "").strip()
    if not text:
        return []
    candidates = [text]
    bare = _EMOJI_PREFIX_RE.sub("", text).strip()
    if bare and bare != text:
        candidates.append(bare)
    return candidates


class MenuTapFilter(filters.MessageFilter):
    """``True`` for text that IS a reply-keyboard tap -- asked of the ONE decider.

    Two questions, one rule, one implementation
    (``StaffBot._resolve_tapped_label``):

    * ``translation_key=None`` -- "is this ANY main-menu tap?". Every
      ConversationHandler text state is guarded by this so a menu tap ends the
      flow instead of being swallowed as the phone number / bottle count / cash
      amount the state was waiting for.
    * a key -- "is this a tap on THAT button?", for the three operator labels
      (Create Client / Search Client / Create Order) that ENTER a conversation
      of their own instead of routing through ``_dispatch_menu_action``.

    Both answer by asking the matcher, so "the filter claimed it" and "the
    matcher resolved it" can no longer be different answers -- which is what
    made a conversation die with zero output (see ``_EMOJI_PREFIX_RE`` above).

    The lookup happens HERE, when the tap arrives, never at handler-build time.
    The keyboard resolves its labels at RENDER time, so a label edited in the
    admin UI (or any ``i18n.reload_translations()``) is on the staff member's
    phone immediately; a matcher frozen at startup would keep hunting for the
    old string and the button they can see would be dead until a restart --
    silently, with the retired copy still live to hijack typed text.
    """

    __slots__ = ("_staff_bot", "_translation_key")

    def __init__(self, staff_bot: "StaffBot", translation_key: Optional[str] = None):
        super().__init__(name="staff_menu_tap:%s" % (translation_key or "main_menu"))
        self._staff_bot = staff_bot
        self._translation_key = translation_key

    def filter(self, message) -> bool:
        if self._translation_key is None:
            return self._staff_bot._match_menu_action(message.text, None) is not None
        return self._staff_bot._match_menu_label(message.text, self._translation_key)


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


class TimedApplication(Application):
    """`Application` subclass that measures how long each update takes end to end.

    Why this exists: on 2026-08-13 staff_bot was reported slow and it was
    IMPOSSIBLE to confirm from telemetry — the bot logged `Update: ...` on
    arrival and then nothing. No elapsed time, no completion marker, so
    "slow" could not be distinguished from "idle". This closes that gap.

    It overrides `process_update` rather than adding another handler because
    that is the only point that sees the WHOLE update: a `TypeHandler` in
    group -10 returns before the real handlers run, and a last-group handler
    is skipped entirely when something raises `ApplicationHandlerStop` or
    when an earlier group errors. The override reports from a `finally:`, so
    a failed update is still measured.

    Emits WARNING above `STAFF_BOT_SLOW_UPDATE_SECONDS` so slow updates are
    greppable in Loki (`slow_update elapsed=`) without turning normal traffic
    into noise at INFO.

    Implementation note: PTB's `Application` defines `__slots__`, so it is
    impossible to install this as an instance attribute
    (`application.process_update = ...` raises `AttributeError: 'Application'
    object attribute 'process_update' is read-only`). Subclassing and wiring
    the subclass in via `ApplicationBuilder.application_class(...)` is the
    supported hook for exactly this.
    """

    async def process_update(self, update):
        started = time.perf_counter()
        try:
            return await super().process_update(update)
        finally:
            elapsed = time.perf_counter() - started
            slow_threshold = float(os.environ.get("STAFF_BOT_SLOW_UPDATE_SECONDS", "3.0"))
            try:
                user_id = update.effective_user.id if getattr(update, "effective_user", None) else "N/A"
                if getattr(update, "callback_query", None):
                    kind = f"callback_query={update.callback_query.data}"
                elif getattr(update, "message", None):
                    kind = "message"
                else:
                    kind = "other"
                if elapsed >= slow_threshold:
                    logger.warning(
                        "slow_update elapsed=%.2fs user=%s %s "
                        "(threshold %.1fs; with concurrent_updates disabled this "
                        "also delays every other staff member's update)",
                        elapsed, user_id, kind, slow_threshold,
                    )
                else:
                    logger.info("update_processed elapsed=%.3fs user=%s %s", elapsed, user_id, kind)
            except Exception:  # noqa: BLE001
                # Instrumentation must never break update processing.
                logger.debug("update timing log failed", exc_info=True)


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

            # Route-card state (Plan 3): same Redis, same connected/None
            # fallback as flow_state above -- the card must keep editing
            # the SAME message across a bot restart, which requires Redis.
            # Redis is the ONLY store: without it the module degrades to
            # "no card state", exactly like flow_state, rather than keeping
            # a second in-process copy that could disagree with Redis.
            from staff_bot.utils import route_card_state
            route_card_state.configure(self.token_manager.redis if self.token_manager._connected else None)

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

            # concurrent_updates: PTB's default is False, which processes every
            # update STRICTLY SEQUENTIALLY across all staff. One slow handler
            # then stalls every other driver and operator — the amplifier
            # behind the 2026-08-13 "staff bot is slow" report, where several
            # drivers were active in overlapping windows and two of them gave
            # up and re-sent /start. It is also the mechanism this repo already
            # documents in telegram_bot/handlers/callback_dedup.py, where
            # serial queuing turns an impatient double-tap into a duplicate
            # message.
            #
            # We deliberately do NOT pass a bare True/int here. PTB's own docs
            # warn that blanket concurrency is unsafe with stateful handlers,
            # and this bot is full of ConversationHandlers (bottle collection,
            # cash collection, try-outs, operator order entry) — that would
            # swap a bot-wide stall for a single driver racing their own
            # conversation state in flows that move money and bottles.
            #
            # PerChatSerialUpdateProcessor keeps each chat strictly ordered
            # (so ConversationHandler sees the sequential world it expects)
            # while letting DIFFERENT staff run in parallel, which is the
            # actual fix. Concurrency stays bounded by max_concurrent_updates.
            self.application = (
                Application.builder()
                .application_class(TimedApplication)
                .token(config.telegram.bot_token)
                .request(request)
                .get_updates_request(get_updates_request)
                .concurrent_updates(
                    PerChatSerialUpdateProcessor(
                        max_concurrent_updates=int(
                            os.environ.get("STAFF_BOT_CONCURRENT_UPDATES", "16")
                        )
                    )
                )
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

    @require_auth
    async def _route_new_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Route the unified `New Orders` entry to the correct pool view based on staff role.
        Dual-role users default to the delivery driver (actionable) view.

        The `else` is not defensive padding: `_normalize_staff_roles` returns
        `[]` whenever a re-auth response omits `staff_roles`
        (handlers/base.py:197), and without a fallback the second
        reply-keyboard button was silently inert for exactly those users
        (spec §4.6).
        """
        roles = context.user_data.get('staff_roles', []) or []
        if 'delivery_driver' in roles:
            await self._delivery_handlers['orders_pool'].show_pool(update, context)
        elif 'operator' in roles:
            await self._operator_handlers['orders_pool_view'].show_pool(update, context)
        else:
            language = await self._language_handler._get_language(update, context)
            message = i18n.get('staff.session_expired', language)
            if update.callback_query:
                await update.callback_query.answer(message, show_alert=True)
            elif update.message:
                # Silent like every other staff_bot send: only the head-change
                # alert is allowed to make a sound (drivers are driving).
                await update.message.reply_text(message, disable_notification=True)

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
        redispatch_handler = RedispatchHandler()

        # Common handlers
        profile_handler = ProfileHandler()
        help_handler_instance = HelpHandler()

        # ------------------------------------------------------------------
        # The four ways a staff member leaves a flow
        # ------------------------------------------------------------------
        # A reply-keyboard MENU TAP (`menu_escape`, below), a NAVIGATION BUTTON
        # tapped inside the flow, `/cancel`, and `/start`. Every one of them
        # runs `self._clear_all_pending_flows` and returns
        # `ConversationHandler.END`; none of them has its own idea of what
        # "left" means. A fallback that returns None instead of END re-renders
        # its message and keeps the driver exactly where they were -- the
        # BOTTLE_SESSION_REQUIRED trap this bot already shipped, where "Open
        # session" -> quantity prompt -> inline Back kept asking for the
        # quantity forever.
        async def _leave_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """A navigation button (`staff_cash_hub` / `staff_back_to_main`)
            tapped INSIDE a conversation. Ends it and renders nothing.

            Rendering is not this handler's job: `show_cash_hub` and
            `main_menu_handler` are registered at the bottom of this method in
            group 1 -- AFTER every conversation -- so the destination is drawn
            exactly once, by the one handler that owns it, whether or not the
            staff member happened to be in a flow. Drawing it here as well is
            what made an inline Back inside a bottle flow edit the same message
            twice and log a Telegram "message is not modified" for every tap.

            The group also matters the other way round: `staff_cash_hub` used
            to be registered in group 0 BEFORE the conversations, and PTB
            handles at most one handler per group, so the global hub shadowed
            the conversation fallback that would have ended the flow. The
            driver tapped Back, the hub rendered, and the flow stayed armed to
            swallow the next number they typed.
            """
            await self._clear_all_pending_flows(context, update)
            return ConversationHandler.END

        async def _cancel_bottle_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """`/cancel` inside one of the five bottle conversations.

            They have no cancel handler of their own, so this used to be wired
            to `start_handler.cancel` -- written for abandoning LOGIN. It
            answered "Authentication cancelled." (untrue: the driver is still
            logged in) and, far worse, sent `ReplyKeyboardRemove`.
            `MenuKeyboards.main_menu` is built `is_persistent=True` precisely
            because a driver's control surface must always be on screen; one
            `/cancel` at a bottle-count prompt took it away, from the road,
            with no visible buttons left to guess a way back from.
            """
            language = await self._language_handler._get_language(update, context)
            await self._clear_all_pending_flows(context, update)
            message = update.effective_message
            if message is not None:
                await message.reply_text(
                    i18n.get('staff.bottle_flow_cancelled', language),
                    reply_markup=MenuKeyboards.main_menu(
                        language, context.user_data.get('staff_roles', []) or []
                    ),
                    # Drivers are driving: only the head-change alert may ping.
                    disable_notification=True,
                )
            return ConversationHandler.END

        async def _start_is_a_hard_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """`/start` inside any flow: end it.

            `StartHandler.start` calls itself "a hard reset to the top of the
            bot" and clears every flow flag, but it lives in the `staff_auth`
            conversation in group -2 and could not touch the ten conversations
            in group 0 -- they listed only `/cancel` as a fallback, so nothing
            in them matched `/start` at all. The driver saw "Welcome back" and
            the main menu and believed they were back at the top, while the
            prompt they had walked away from was still armed and still
            outranked the catch-all router: the next bare number they typed
            opened a real bottle session against their name.

            Renders nothing -- `StartHandler.start` has already answered in
            group -2, and both groups run for the same update.
            """
            await self._clear_all_pending_flows(context, update)
            return ConversationHandler.END

        start_reset = CommandHandler("start", _start_is_a_hard_reset)

        def _flow_timeout(*, offer_menu: bool = True):
            """The `ConversationHandler.TIMEOUT` state for one staff flow.

            `conversation_timeout` is not self-announcing: when the timer fires
            PTB looks for handlers under the TIMEOUT key and, finding none,
            ends the conversation in TOTAL SILENCE. The staff member is left on
            a prompt whose buttons are dead and whose keys survive in
            `user_data` for the next flow to trip over -- the same defect that
            lost 20 of 33 customer addresses in the customer bot's
            address_conversation. Eleven staff conversations set a timeout and
            none of them said so.

            One factory rather than eleven copies: "say so, drop the flow's
            keys, end" is one rule, and the only per-flow part is whether the
            person has a main menu to be handed back to (`staff_auth` parks
            someone who is not linked to a staff account yet, and the step they
            abandoned left a language reply-keyboard on their screen -- taking
            it away is the only honest thing to show).

            BOTH a MessageHandler and a CallbackQueryHandler: PTB re-dispatches
            the LAST update the staff member sent, so a flow abandoned on an
            inline button times out on a callback query and one abandoned at a
            text prompt times out on a message. A TIMEOUT state that registers
            only one of the two is silent for half the flows that reach it.

            Deliberately NOT raising ApplicationHandlerStop: PTB dispatches
            TIMEOUT handlers itself and documents that it has no effect there.
            """
            async def _announce_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
                try:
                    language = await self._language_handler._get_language(update, context)
                    await self._clear_all_pending_flows(context, update)
                    text = i18n.get('staff.flow_timed_out', language)
                    keyboard = (
                        MenuKeyboards.main_menu(
                            language, context.user_data.get('staff_roles', []) or []
                        )
                        if offer_menu else ReplyKeyboardRemove()
                    )
                    # The timeout update is the staff member's LAST real one, so
                    # the reply target is derived rather than assumed.
                    query = update.callback_query
                    target = query.message if query is not None else update.effective_message
                    if target is not None:
                        await target.reply_text(
                            text, reply_markup=keyboard, disable_notification=True
                        )
                    elif update.effective_user is not None:
                        await context.bot.send_message(
                            chat_id=update.effective_user.id, text=text,
                            reply_markup=keyboard, disable_notification=True,
                        )
                except Exception as exc:  # noqa: BLE001 -- never surface to the driver
                    logger.error(
                        "Failed to announce staff conversation timeout: %s", exc, exc_info=True
                    )
                return ConversationHandler.END

            return [
                MessageHandler(filters.ALL, _announce_timeout),
                CallbackQueryHandler(_announce_timeout),
            ]

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
            'redispatch': redispatch_handler,
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
                # Not linked to a staff account yet, so no main menu to hand back.
                ConversationHandler.TIMEOUT: _flow_timeout(offer_menu=False),
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
            # `staff_cash_hub` is deliberately NOT here -- it is registered in
            # group 1, below the conversations. See the note there.

            # --- Delivery handlers ---
            # Order pool
            CallbackQueryHandler(orders_pool_handler.show_pool, pattern="^staff_new_orders$"),
            CallbackQueryHandler(orders_pool_handler.view_order_details, pattern=r"^staff_view_order_\d+$"),
            CallbackQueryHandler(orders_pool_handler.accept_order, pattern=r"^staff_accept_order_\d+$"),
            CallbackQueryHandler(orders_pool_handler.confirm_accept, pattern=r"^staff_confirm_accept_\d+$"),
            CallbackQueryHandler(orders_pool_handler.pool_pagination, pattern=r"^staff_pool_page_\d+$"),

            # Active deliveries
            CallbackQueryHandler(active_delivery_handler.show_active_deliveries, pattern="^staff_active_deliveries$"),
            CallbackQueryHandler(active_delivery_handler.refresh_route_card, pattern="^staff_route_refresh$"),
            CallbackQueryHandler(active_delivery_handler.switch_route_view, pattern="^staff_route_view_(next|all)$"),
            CallbackQueryHandler(active_delivery_handler.view_active_delivery, pattern=r"^staff_view_active_\d+$"),
            CallbackQueryHandler(active_delivery_handler.navigate_to_address, pattern=r"^staff_navigate_\d+$"),
            CallbackQueryHandler(active_delivery_handler.optimize_routes, pattern="^staff_optimize_routes$"),
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
            # The handoff button carries the amount it displayed: "…submit_all:120000.00".
            # The bare form is still routed so buttons rendered before that
            # change keep reaching the handler (which redraws instead of writing).
            CallbackQueryHandler(status_update_handler.submit_reconciliation_all, pattern=r"^staff_reconcile_submit_all(:|$)"),
            CallbackQueryHandler(status_update_handler.start_reconciliation_submit, pattern="^staff_reconcile_submit$"),
            CallbackQueryHandler(cash_collection_handler.show_debtor_list, pattern="^staff_cod_collect_menu$"),
            CallbackQueryHandler(cash_collection_handler.paginate_debtor_list, pattern=r"^staff_cod_list_page_\d+$"),
            CallbackQueryHandler(cash_collection_handler.show_customer_statement, pattern=r"^staff_cod_customer_\d+$"),
            CallbackQueryHandler(cash_collection_handler.start_full_collection, pattern=r"^staff_cod_collect_full_\d+$"),
            CallbackQueryHandler(cash_collection_handler.start_custom_collection, pattern=r"^staff_cod_collect_custom_\d+$"),
            CallbackQueryHandler(cash_collection_handler.confirm_overpayment_collection, pattern=r"^staff_cod_confirm_overpay_yes$"),
            CallbackQueryHandler(cash_collection_handler.cancel_overpayment_collection, pattern=r"^staff_cod_confirm_overpay_no$"),
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
            CallbackQueryHandler(redispatch_handler.show_failed_deliveries, pattern="^staff_redispatch_failed$"),
            CallbackQueryHandler(redispatch_handler.redispatch_delivery, pattern=r"^staff_redispatch_do_\d+$"),

            # --- Common handlers ---
            CallbackQueryHandler(profile_handler.show_profile, pattern="^staff_profile$"),
            CallbackQueryHandler(help_handler_instance.show_help, pattern="^staff_help$"),

            # Noop (pagination current page indicator)
            CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"),
        ]

        for handler in callback_handlers:
            self.application.add_handler(handler)

        # --- Operator conversation handlers ---
        # The three operator labels that ENTER a conversation instead of
        # routing. Same decider as the escape below, only narrowed to one
        # button each -- and, like the escape, it reads the translation when
        # the tap ARRIVES. These used to be regexes compiled from `i18n` right
        # here, at handler-build time, so retitling one of these buttons in the
        # admin UI rendered new copy on a keyboard nothing would answer until
        # the bot was restarted.
        create_client_tap = self._menu_label_tap_filter('staff.menu.create_client')
        search_client_tap = self._menu_label_tap_filter('staff.menu.search_client')
        create_order_tap = self._menu_label_tap_filter('staff.menu.create_order')

        # Reply-keyboard MAIN-MENU escape, registered on EVERY state of every
        # conversation below. A menu tap while typing (phone/name/address/note/
        # qty) would otherwise be captured as that input by the state's own
        # MessageHandler -- which wins over the catch-all menu router -- and a
        # menu tap on a callback-only step (the language picker, the confirm
        # card, the transfer driver picker) reached no handler at all, so the
        # destination opened while the conversation stayed armed behind it.
        #
        # The guard is `MenuTapFilter`, i.e. `_match_menu_action` itself.
        # Text that is not a menu tap is never claimed here and falls through
        # to the state's real receive_* handler; text that IS one can always be
        # resolved, so this can no longer end a flow with nothing to show for it.
        menu_escape = MessageHandler(
            self._main_menu_tap_filter() & ~filters.COMMAND, self._conv_menu_escape
        )

        # Create User conversation
        create_user_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(create_user_handler.start_create_user, pattern="^staff_create_client$"),
                CallbackQueryHandler(create_user_handler.start_create_user, pattern="^staff_op_create_user$"),
                MessageHandler(
                    create_client_tap & ~filters.COMMAND,
                    create_user_handler.start_create_user
                ),
            ],
            states={
                ENTER_PHONE: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_phone)
                ],
                ENTER_FIRST_NAME: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_first_name)
                ],
                ENTER_LAST_NAME: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_handler.receive_last_name)
                ],
                CREATE_USER_LANG: [
                    menu_escape,
                    CallbackQueryHandler(create_user_handler.select_client_language, pattern=r"^staff_op_lang_")
                ],
                CONFIRM_CREATE: [
                    menu_escape,
                    CallbackQueryHandler(create_user_handler.confirm_create, pattern="^staff_op_confirm_create_user$")
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", create_user_handler.cancel),
                start_reset,
                CallbackQueryHandler(create_user_handler.cancel, pattern="^staff_back_to_main$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
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
                    search_client_tap & ~filters.COMMAND,
                    search_user_handler.start_search
                ),
            ],
            states={
                SEARCH_INPUT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, search_user_handler.receive_search_query)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", search_user_handler.cancel),
                start_reset,
                CallbackQueryHandler(search_user_handler.cancel, pattern="^staff_back_to_main$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
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
                    create_order_tap & ~filters.COMMAND,
                    create_order_handler.start_create_order
                ),
            ],
            states={
                ORDER_SELECT_CLIENT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_order_handler.receive_client_search)
                ],
                ORDER_SELECT_ADDRESS: [
                    menu_escape,
                    CallbackQueryHandler(create_order_handler.select_address, pattern=r"^staff_op_addr_\d+$"),
                ],
                ORDER_SELECT_PRODUCTS: [
                    menu_escape,
                    CallbackQueryHandler(create_order_handler.select_product, pattern=r"^staff_op_product_\d+$"),
                    CallbackQueryHandler(create_order_handler.products_done, pattern="^staff_op_products_done$"),
                ],
                ORDER_SELECT_QUANTITY: [
                    menu_escape,
                    CallbackQueryHandler(create_order_handler.select_quantity, pattern=r"^staff_op_qty_\d+_\d+$"),
                ],
                ORDER_SELECT_PAYMENT: [
                    menu_escape,
                    CallbackQueryHandler(create_order_handler.select_payment, pattern=r"^staff_op_pay_"),
                ],
                ORDER_ENTER_NOTES: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, create_order_handler.receive_notes),
                    CallbackQueryHandler(create_order_handler.skip_notes, pattern="^staff_op_skip_notes$"),
                ],
                ORDER_CONFIRM_ORDER: [
                    menu_escape,
                    CallbackQueryHandler(create_order_handler.confirm_order, pattern="^staff_op_confirm_order$"),
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", create_order_handler.cancel),
                start_reset,
                CallbackQueryHandler(create_order_handler.cancel, pattern="^staff_back_to_main$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_label)
                ],
                ENTER_ADDRESS: [
                    menu_escape,
                    # The pin half of the address step. Without it a shared
                    # location reaches no handler in this state and the operator
                    # taps the bot's own "Send location" button into silence —
                    # and, worse, the address is then saved with no coordinates,
                    # which is precisely what makes the delivery-zone guard
                    # (`ensure_within_delivery_zone`) a no-op on this path.
                    MessageHandler(filters.LOCATION, manage_address_handler.receive_location),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_address)
                ],
                ENTER_DISTRICT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_district)
                ],
                ADDR_ENTER_NOTES: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, manage_address_handler.receive_address_notes)
                ],
                CONFIRM_ADDRESS: [
                    menu_escape,
                    CallbackQueryHandler(manage_address_handler.confirm_address, pattern="^staff_op_confirm_address$")
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", manage_address_handler.cancel),
                start_reset,
                CallbackQueryHandler(manage_address_handler.cancel, pattern="^staff_back_to_main$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_phone)
                ],
                ENTER_TRYOUT_NAME: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_name)
                ],
                ENTER_TRYOUT_ADDRESS: [
                    menu_escape,
                    MessageHandler(filters.LOCATION, tryout_handler.receive_create_location),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_address)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", tryout_handler.cancel_create_tryout),
                start_reset,
                CallbackQueryHandler(tryout_handler.cancel_create_tryout, pattern="^staff_back_to_main$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_collection_search)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", _cancel_bottle_flow),
                start_reset,
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_back_to_main$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_loaded)
                ],
                BOTTLE_SESSION_LOADED_QTY_INPUT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_loaded)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", _cancel_bottle_flow),
                start_reset,
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_back_to_main$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_returned)
                ],
                BOTTLE_SESSION_RETURNED_QTY_INPUT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_bottles_returned)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", _cancel_bottle_flow),
                start_reset,
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_back_to_main$"),
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
                # A driver button tapped from a picker the driver already walked
                # away from. Now that a menu tap ENDS this conversation, the
                # buttons still sitting in the scrollback reach no state -- and a
                # tap that lands nowhere is the failure this wave is about.
                # `receive_transfer_driver_select` refuses out loud when
                # `pending_transfer_available` is gone (i.e. the flow is over)
                # and re-renders the session menu. Registered as an entry point,
                # not a second global handler, so the LIVE flow and the stale tap
                # go to the same function and cannot answer differently.
                CallbackQueryHandler(bottle_collection_handler.receive_transfer_driver_select, pattern=r"^staff_transfer_driver_\d+$"),
            ],
            states={
                BOTTLE_TRANSFER_DRIVER_SELECT: [
                    menu_escape,
                    CallbackQueryHandler(bottle_collection_handler.receive_transfer_driver_select, pattern=r"^staff_transfer_driver_\d+$"),
                ],
                BOTTLE_TRANSFER_QTY_INPUT: [
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_transfer_quantity)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", _cancel_bottle_flow),
                start_reset,
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_back_to_main$"),
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
                    menu_escape,
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bottle_collection_handler.receive_transfer_custom_confirm)
                ],
                ConversationHandler.TIMEOUT: _flow_timeout(),
            },
            fallbacks=[
                CommandHandler("cancel", _cancel_bottle_flow),
                start_reset,
                CallbackQueryHandler(_leave_conversation, pattern="^staff_cash_hub$"),
                CallbackQueryHandler(_leave_conversation, pattern="^staff_back_to_main$"),
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

        # ------------------------------------------------------------------
        # Navigation destinations: registered AFTER the conversations, always
        # ------------------------------------------------------------------
        # PTB runs at most ONE handler per group and `handlers[group]` is
        # insertion-ordered, so a global handler registered in group 0 before a
        # ConversationHandler SHADOWS that conversation's fallback for the same
        # callback data. `staff_cash_hub` was registered up there with the other
        # callbacks: the driver tapped the cash hub's own back button from
        # inside a bottle flow, the hub rendered, the conversation never got the
        # update, and it stayed armed until their next typed number opened or
        # closed a bottle session.
        #
        # Group 1 fixes it in both directions: the conversation's fallback ends
        # the flow in group 0, and these render the destination in group 1 --
        # once, from one place, whether or not a flow was open.
        self.application.add_handler(
            CallbackQueryHandler(main_menu_handler, pattern="^staff_back_to_main$"),
            group=1
        )
        self.application.add_handler(
            CallbackQueryHandler(status_update_handler.show_cash_hub, pattern="^staff_cash_hub$"),
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
            # SSOT: clears every flow flag + the Redis mirror and drains any
            # pool-insertion suggestions deferred while the user was mid-flow.
            await self._clear_all_pending_flows(context, update)
            # Land on the cash hub — every current flow that uses these flags
            # is reachable from there, so it's the least-surprising parent.
            await status_update_handler.show_cash_hub(update, context)

        self.application.add_handler(
            CallbackQueryHandler(_handle_flow_cancel, pattern="^staff_flow_cancel$")
        )

        # Location handler for live location updates. Private chats only:
        # request_location buttons are inert in groups, and a group member's
        # stray pin must never be written as a driver's position.
        self.application.add_handler(
            MessageHandler(
                filters.LOCATION & filters.ChatType.PRIVATE,
                location_handler.handle_location_update,
            )
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
            # This is what most drivers actually see in the `/` menu, so it must
            # be the deployment default language (uz), not English.
            default_language = i18n.normalize_language(None)
            fallback_commands = [
                BotCommand("start", i18n.get('staff.command.start', default_language)),
                BotCommand("menu", i18n.get('staff.command.menu', default_language)),
                BotCommand("help", i18n.get('staff.command.help', default_language)),
                BotCommand("language", i18n.get('staff.command.language', default_language)),
            ]
            await self.application.bot.set_my_commands(fallback_commands)
            logger.info("Staff bot commands set successfully")
        except Exception as e:
            logger.error(f"Failed to set bot commands: {e}")

    def _menu_action_map(self, language: str) -> dict:
        """Reply-keyboard MAIN-MENU label -> action, for the labels that have no
        dedicated handler and fall through to _handle_text_message. (Operator
        labels Create Client / Search Client / Create Order are handled by their
        own ConversationHandlers and are intentionally absent here.)

        Rebuilt on every lookup, never cached: `i18n.get` is what the KEYBOARD
        calls at render time too, so an admin retitling a button is answered by
        the very next tap rather than by the next restart.

        Keys are STRIPPED, on both sides of the comparison
        (`_menu_tap_candidates` strips what arrived). This map used the RAW
        translation row. A row seeded as "Cash " therefore rendered a button
        that the escape recognised as navigation and this map resolved to
        nothing -- outside a conversation the driver was bounced back to the
        menu, inside one their flow died silently. One invisible trailing space
        in a translations row was enough, and nothing on screen showed it.
        """
        raw = {
            i18n.get('staff.menu.new_orders', language): 'staff_new_orders_unified',
            i18n.get('staff.menu.active_deliveries', language): 'staff_active_deliveries',
            i18n.get('staff.menu.tryouts', language): 'staff_tryouts_hub',
            i18n.get('staff.menu.cash', language): 'staff_cash_hub',
            i18n.get('staff.menu.profile', language): 'staff_profile',
            i18n.get('staff.menu.settings', language): 'staff_settings',
            i18n.get('staff.menu.help', language): 'staff_help',
        }
        return {
            label.strip(): action
            for label, action in raw.items()
            if label and label.strip()
        }

    @staticmethod
    def _single_menu_label_map(translation_key: str, language: str) -> dict:
        """`_menu_action_map`'s shape for ONE button, so the two kinds of menu
        label are matched by the same code.

        The "action" is the translation key itself: nothing routes on it -- the
        only caller asks whether the tap resolved at all -- but returning it
        keeps `_resolve_tapped_label` a single function with a single contract.
        """
        label = i18n.get(translation_key, language).strip()
        return {label: translation_key} if label else {}

    def _main_menu_tap_filter(self) -> "MenuTapFilter":
        """The filter every ConversationHandler state is guarded by.

        A method rather than a module constant so it binds to THIS bot and its
        handler instances, and so the escape wiring reads as one thing:
        `menu_escape = MessageHandler(self._main_menu_tap_filter() & ...)`.
        """
        return MenuTapFilter(self)

    def _menu_label_tap_filter(self, translation_key: str) -> "MenuTapFilter":
        """The filter for ONE reply-keyboard button, by translation key.

        Used by the three operator labels that ENTER a ConversationHandler of
        their own; the same decider as the escape, narrowed to one button.
        """
        return MenuTapFilter(self, translation_key)

    async def _leave_flow_and_navigate(
        self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Leave the flow AND take the staff member where they tapped.

        The shared body of the two menu-tap routes. Keeping them one function
        is the point: a tap answered by the conversation escape and the same
        tap answered by the catch-all router must be indistinguishable to the
        person who made it.
        """
        await self._clear_all_pending_flows(context, update)
        await self._dispatch_menu_action(action, update, context)
        # AFTER the dispatch, never before -- see _delete_menu_echo. A menu tap
        # made INSIDE a conversation leaves exactly the same echo as one made
        # outside it; without this it piles up and buries the pinned card.
        await self._delete_menu_echo(update)

    async def _conv_menu_escape(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply-keyboard MAIN-MENU tap fired inside a ConversationHandler state.

        Abandon the conversation: clear its half-entered working data and any
        flow flags, navigate to the tapped menu action, and END so the stale
        state can't capture the staff member's next text. Registered on EVERY
        state of every conversation -- the callback-only ones too, because a
        driver parked on an inline-button step who taps a main-menu button has
        left just as definitely as one parked on a text prompt, and while the
        conversation stayed armed every inline button on its last message
        stayed live for the rest of the five-minute timeout.

        Non-menu text still reaches the real receive_* handler: the guard is
        `MenuTapFilter`, which asks `_match_menu_action` -- so a text this
        cannot resolve is never claimed here in the first place.

        Reads via `effective_message`, exactly like `_handle_text_message` and
        `_delete_menu_echo`: the `filters.TEXT` states this sits in front of
        also match EDITED messages, where `update.message` is None. Without
        this an edited menu-label tap raised AttributeError here -- and now
        that this method deletes the echo too, it would blow up before ever
        reaching that cleanup."""
        message = update.effective_message
        if message is None:
            # No message payload at all: nothing to match against and nothing
            # to delete. Still END -- the tap that routed here was claimed as a
            # menu tap, so the driver has left this conversation whether or not
            # we can read the text; leaving the state armed would let it
            # capture their next message. (Unreachable in practice: the
            # registered filter matches on effective_message, so it is
            # non-None whenever this handler actually runs.)
            return ConversationHandler.END
        language = await self._language_handler._get_language(update, context)
        action = self._match_menu_action(message.text, language)
        if action is None:
            # Unreachable through the registered filter, which IS this matcher.
            # It used to be reachable -- the filter allowed any leading token
            # while the matcher allowed 2-4 characters -- and the conversation
            # was then torn down with no output whatsoever. Stay put and say
            # nothing rather than repeat that; the state's own handler is the
            # right owner of text this cannot resolve.
            logger.warning(
                "menu escape claimed text it cannot resolve (%r); staying in the flow",
                message.text,
            )
            return None
        await self._leave_flow_and_navigate(action, update, context)
        return ConversationHandler.END

    def _resolve_tapped_label(self, text: str, label_map, language: str = None):
        """Which button is this text a tap on? THE rule, and the only copy of it.

        `label_map(language) -> {stripped label: action}` is the only thing the
        two kinds of menu button differ by: `_menu_action_map` for the labels
        that route, `_single_menu_label_map` for the three operator labels that
        enter a conversation of their own. Everything that decides whether a
        text IS a tap -- the emoji strip, the stripped compare, the
        cross-language sweep -- lives here, so a change to the rule cannot
        reach one kind of button and miss the other.

        Resolved WHEN THE TAP ARRIVES: `label_map` calls `i18n.get`, exactly as
        the keyboard builder does when it renders. Nothing here is memoised
        across updates, and that is the feature -- an admin retitling a label
        (or `i18n.reload_translations()`) changes what the button says AND what
        answers it in the same instant, with no restart, and the retired copy
        stops matching at the same instant so it cannot hijack typed text.

        Accepts the bare label and the emoji-prefixed variant the reply
        keyboard emits (e.g. '<emoji> Cash'); see `_menu_tap_candidates` for
        why the prefix must be an emoji and not "a couple of characters".

        Resolved across EVERY supported language, not just `language`: a
        keyboard rendered before a language switch lives on the phone until
        Telegram redraws it, and every tap in that window arrives in the old
        language. `language` only decides which action wins if two languages
        happen to render the same label for different buttons -- hence its
        table is consulted first, and each language's table is built at most
        once per call and only if the languages before it did not answer.

        Cost, measured rather than assumed: one `i18n.get` -- an in-memory dict
        lookup -- per label per language, each language's table built at most
        once per call and the sweep abandoned on the first hit. The BARE label
        is the candidate that normally matches (the keyboard adds the emoji),
        and it is only tried once the decorated one has been ruled out
        everywhere, so a main-menu tap usually visits every language: 7 labels
        x 3 languages = 21 lookups, ~45us -- the same as it cost before, when
        only the entry-point labels were frozen. Each of the three operator
        entry filters adds 1 x 3 = 3 lookups (~9us) in place of one compiled
        regex. Per text update that is well under a millisecond, against a
        staff bot whose whole text volume is a handful of drivers.
        """
        candidates = _menu_tap_candidates(text)
        if not candidates:
            return None
        if language:
            language = i18n.normalize_language(language)
        languages = ([language] if language else []) + [
            lang_code
            for lang_code in i18n.supported_languages
            if lang_code != language
        ]
        tables = {}
        for candidate in candidates:
            for lang_code in languages:
                if lang_code not in tables:
                    tables[lang_code] = label_map(lang_code)
                action = tables[lang_code].get(candidate)
                if action is not None:
                    return action
        return None

    def _match_menu_action(self, text: str, language: str = None):
        """Return the menu action for a reply-keyboard main-menu label tap, or None.

        THE decider for "is this text a main-menu tap, and which button?".
        `MenuTapFilter` -- the escape hatch guarding every conversation state --
        asks this and nothing else, and so does the catch-all router and the
        post-restart session-recovery gate. There is deliberately no second,
        looser predicate: when this returns None the text was never a tap, and
        it must fall through to whatever the staff member was actually being
        asked for.

        All menu labels are non-numeric, so a typed cash amount / bottle count
        / fine quantity can NEVER collide; the residual collision is a
        free-text note that literally equals a menu label in some supported
        language, which we accept as an intentional escape hatch.
        """
        return self._resolve_tapped_label(text, self._menu_action_map, language)

    def _match_menu_label(self, text: str, translation_key: str, language: str = None) -> bool:
        """Is this text a tap on the ONE button rendered from `translation_key`?

        The question the three operator entry points ask -- they own a
        conversation rather than an action, so they need "was it THIS button",
        not "which button". Same decider, same emoji/strip/cross-language rule
        as `_match_menu_action`, narrowed to a one-row table.
        """
        return self._resolve_tapped_label(
            text,
            lambda lang_code: self._single_menu_label_map(translation_key, lang_code),
            language,
        ) is not None

    async def _dispatch_menu_action(self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route a matched main-menu action to its handler."""
        handler_map = {
            'staff_new_orders_unified': self._route_new_orders,
            'staff_tryouts_hub': self._delivery_handlers['tryouts'].show_hub,
            'staff_cash_hub': self._delivery_handlers['status_update'].show_cash_hub,
            'staff_active_deliveries': self._delivery_handlers['active_delivery'].show_active_deliveries,
            'staff_profile': self._common_handlers['profile'].show_profile,
            'staff_settings': None,  # Handled by language_handler menu below.
            'staff_help': self._common_handlers['help'].show_help,
        }
        handler_func = handler_map.get(action)
        if handler_func:
            await handler_func(update, context)
        elif action == 'staff_settings':
            await self._language_handler.language_menu(update, context)

    async def _delete_menu_echo(self, update: Update) -> None:
        """Remove the driver's own reply-keyboard tap from the chat.

        The reply keyboard sends TEXT, so every tap leaves a message. Left
        alone they pile up, bury the pinned route card, and turn a chat that
        is meant to work like an app into a transcript of the driver talking
        to themselves (tap-feedback spec §4.1).

        Called AFTER the dispatched handler has produced its output, never
        before: if the render fails, the driver must still see the message
        they sent rather than a tap that vanished into nothing.

        Telegram documents "Bots can delete incoming messages in private
        chats", limited to messages sent less than 48 hours ago. Entirely
        best-effort -- a failed delete must never surface to the driver or
        undo navigation that already succeeded.

        The echo's id is captured BEFORE the delete: `note_echo_deleted` only
        counts echoes that sat BELOW the card, so it needs the id, and the
        Message object is not guaranteed to be useful afterwards.

        Both awaits are individually guarded. The bookkeeping call is
        best-effort in exactly the same sense as the delete, and letting it
        escape would hand `_handle_text_message` an exception AFTER the
        driver's navigation already succeeded -- PTB's global error_handler
        would then apologise for a tap that worked.

        Reads `effective_message` so all four text-router sites agree (the two
        in `_handle_text_message`, one in `_conv_menu_escape`, this one), and
        so an EDITED menu-label tap has its echo cleaned up too rather than
        silently accumulating.

        CALLBACK GUARD, load-bearing: for a callback-query update
        `effective_message` resolves to `callback_query.message` -- the BOT's
        own message, which for a driver is the PINNED ROUTE CARD. Both
        callers are `filters.TEXT` MessageHandlers so this cannot happen
        today, but the cost of it ever happening is deleting the very card
        this whole branch exists to keep alive, so it is checked rather than
        assumed.
        """
        if update.callback_query is not None:
            return
        message = update.effective_message
        if message is None:
            return
        echo_message_id = getattr(message, 'message_id', None)
        try:
            await message.delete()
        except Exception as exc:  # noqa: BLE001 -- past 48h, already gone, no rights
            logger.debug("menu echo delete skipped: %s", exc)
            return
        user = update.effective_user
        if user is None or echo_message_id is None:
            return
        from staff_bot.utils import route_card_state
        try:
            await route_card_state.note_echo_deleted(user.id, echo_message_id)
        except Exception as exc:  # noqa: BLE001 -- bookkeeping only, never the driver's problem
            logger.debug("menu echo counter update skipped: %s", exc)

    RECOVERY_RETRY_COOLDOWN_SECONDS = 60

    def _recovery_cooldowns(self) -> dict:
        """The per-user 'do not retry recovery yet' map, pruned of expired
        entries on every access.

        Without the prune this is an unbounded instance dict keyed by
        telegram user id: anyone sending a menu label from a fresh account
        adds a permanent entry, and staff_bot installs no rate limiter. It
        runs on a memory-constrained Raspberry Pi 5, so bound it to the only
        thing it needs to remember -- users whose recovery failed inside the
        last RECOVERY_RETRY_COOLDOWN_SECONDS. An expired entry is
        indistinguishable from an absent one (both allow a retry), so
        dropping it changes no behaviour.
        """
        cooldown = getattr(self, '_recovery_cooldown_until', None)
        if cooldown is None:
            cooldown = self._recovery_cooldown_until = {}
            return cooldown
        now = time.monotonic()
        for user_id in [uid for uid, until in cooldown.items() if until <= now]:
            cooldown.pop(user_id, None)
        return cooldown

    def _recovery_on_cooldown(self, user_id: int) -> bool:
        """True while a recently-FAILED session recovery should not be retried.

        Bounds the cost of the recovery path below: without this, one menu
        label replayed in a loop is an unthrottled signed POST to the backend
        auth endpoint per message. Process-local and lost on restart, which is
        fine -- it only ever suppresses a retry, never a first attempt.
        """
        return self._recovery_cooldowns().get(int(user_id), 0.0) > time.monotonic()

    def _note_recovery_failed(self, user_id: int) -> None:
        cooldown = self._recovery_cooldowns()
        cooldown[int(user_id)] = time.monotonic() + self.RECOVERY_RETRY_COOLDOWN_SECONDS

    async def _recover_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Try to re-establish a staff session for a tap that arrived with
        empty `user_data`.

        The Application is built with no PTB persistence, so `user_data`
        dies with the process while the reply keyboard survives on the
        driver's phone.

        Delegates to `BaseHandler._authenticate_staff_session`, which is the
        ONLY path that actually establishes a session: it sets
        `authenticated`, `staff_roles`, `user_id`, the profile fields and the
        cached tokens (handlers/base.py). Deliberately NOT
        `_get_auth_token`: its first branch returns a token straight out of
        the TokenManager's Redis cache without touching `authenticated` or
        `staff_roles`, and Redis outlives a bot restart -- so on the very
        deploy this fix ships for, recovery would hand back a live token
        while `@require_auth` still saw a logged-out user and answered
        'session expired' on every tap, forever. Setting `authenticated`
        here by hand would not do either: `staff_roles` would stay `[]`, so
        `@require_delivery_driver` rejects and `_route_new_orders` falls to
        its no-role branch.

        Rate-bounded: a FAILED recovery is not retried for
        RECOVERY_RETRY_COOLDOWN_SECONDS, so replaying one menu label in a
        loop cannot sustain continuous DB + signed-HTTP load (recovery costs
        an unrate-limited signed POST to /api/staff/auth/login plus the DB
        work behind it).
        """
        from staff_bot.handlers.base import BaseHandler

        user_id = update.effective_user.id
        if self._recovery_on_cooldown(user_id):
            return None

        token_manager = context.bot_data.get('token_manager') if context.bot_data else None
        try:
            token = await BaseHandler()._authenticate_staff_session(update, context, token_manager)
        except Exception as exc:  # noqa: BLE001 -- a dead session is routine
            logger.debug("session recovery failed: %s", exc)
            self._note_recovery_failed(user_id)
            return None

        if not token:
            self._note_recovery_failed(user_id)
        return token

    async def _clear_all_pending_flows(self, context: ContextTypes.DEFAULT_TYPE, update: Update = None):
        """THE expression of "this staff member has left the flow they were in".

        Drops every `user_data` key a flow owns -- the text-router flags, the
        half-entered conversation working data, the loose inline-flow
        breadcrumbs -- plus the Redis flow mirror, draining any pool-insertion
        suggestions deferred while they were busy.

        Every way out calls this and nothing else: a menu tap taken inside a
        ConversationHandler state (`_conv_menu_escape`) and outside one
        (`_handle_text_message`), the inline flow-cancel button, `/cancel`,
        `/start`, a navigation button tapped mid-flow, and a conversation
        timing out. They used to clear different sets, so the SAME gesture left
        different residue depending on which screen it landed on.

        The key list is not here either: it is
        `flow_state.PENDING_FLOW_USER_DATA_KEYS`, the SSOT this delegates to.
        """
        from staff_bot.utils import flow_state as _flow_state
        await _flow_state.clear_pending_flows(context, update)

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (reply keyboard button presses)"""
        # effective_message, not update.message, EVERYWHERE this handler reads
        # the incoming text: PTB's filters.TEXT matches on effective_message,
        # which resolves to edited_message when message is None, so an EDITED
        # text update would raise AttributeError on update.message.text.
        message = update.effective_message
        text = message.text if message is not None else None
        if not context.user_data.get('authenticated'):
            # A reply keyboard outlives the bot process, so a tap can arrive
            # with empty user_data after any restart or deploy. Swallowing it
            # here made every menu button silently dead until the driver
            # guessed /start -- `require_auth`'s own 'session expired' reply
            # (permissions.py:72) never fires, because this router bails first.
            #
            # Scoped to text that IS a menu label, in ANY supported language.
            # Recovery costs a Redis read plus an unrate-limited signed POST to
            # /api/staff/auth/login plus two DB queries; running that for
            # arbitrary text would let anyone who never ran /start drive
            # backend load just by typing at the bot.
            # Same decider as the escape filter and the router below, so the
            # three cannot disagree about what a menu label is.
            if self._match_menu_action(text, None) is None:
                return
            # Captured BEFORE the attempt: a failed recovery ARMS the cooldown,
            # so reading it afterwards would suppress the very first
            # explanation. The reply is gated on the same window as the auth
            # attempt because a driver in a failed-session window taps the
            # dead keyboard repeatedly -- N taps must produce ONE explanation,
            # not N pings and N undeleted messages burying the pinned card.
            # They already got the message; repeating it only adds noise.
            was_on_cooldown = self._recovery_on_cooldown(update.effective_user.id)
            if not await self._recover_session(update, context):
                if message is not None and not was_on_cooldown:
                    language = await self._language_handler._get_language(update, context)
                    await message.reply_text(
                        i18n.get('staff.session_expired', language),
                        # Drivers are driving: only the head-change alert is
                        # allowed to make a sound.
                        disable_notification=True,
                    )
                return

        language = await self._language_handler._get_language(update, context)

        # Reply-keyboard MAIN-MENU taps must always navigate, even mid-flow. The
        # reply keyboard (MenuKeyboards.main_menu) is permanently visible and
        # sends TEXT, so without this guard a tap on Cash / New Orders / etc.
        # while any pending_*_flow is armed is swallowed as flow input — parsed
        # as a cash amount ("Invalid cash amount", the reported bug) or, worse,
        # consumed as a NOTE that finalizes a real transaction. Detect the tap
        # FIRST, drop every in-progress flow, then route to the menu action.
        # Shared body with `_conv_menu_escape`: the same tap must mean the same
        # thing whether the staff member was inside a conversation or not.
        menu_action = self._match_menu_action(text, language)
        if menu_action is not None:
            await self._leave_flow_and_navigate(menu_action, update, context)
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

        # Not a menu label (handled at the top) and no active flow consumed it →
        # unknown free text: fall back to re-rendering the main menu.
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
