"""Leaving unfinished prompts (spec 2026-10-21 §2): the customer bot's flow exit.

Before this module nothing could end a prompt from outside it. Every
conversation lives in group -2 and its text steps stop dispatch (`bot.py`'s
`_consumes`), so a bottom-keyboard label typed into a step became the answer;
/menu and the menu callbacks run in group 0, which an open flow outlives; and
saved prompts in `bot_state.awaiting_input` kept waiting after the customer had
gone somewhere else.

An EXIT GESTURE is one of:

* a bottom-keyboard label, in any supported language (`keyboards.MAIN_MENU_KEYS`
  through `menu_tap.resolve_tapped_label`), or a label the customer still has
  installed (the `menu_keyboard` marker in bot_state, through
  `menu_tap.resolve_installed_label`), so a button the owner has since retitled
  keeps working;
* /start, /menu, /help, /language (`EXIT_COMMANDS`);
* a callback an old inline menu in the customer's chat still sends
  (`LEGACY_MENU_CALLBACK_PATTERN`).

For one, `FlowExitHandler` (group `FLOW_EXIT_GROUP`, after the quantity-screen
middleware and before every conversation) calls `leave_all_flows`, which ends
every open conversation for this chat except signup, runs each flow's own
teardown, and disarms the saved prompts in `PROMPTS_LEFT_BY_EXIT`. It never
stops dispatch and never re-dispatches: the same update then walks on to group
0, where the destination is drawn exactly once. A label is resolved here, once,
and handed to the group-0 router through `pending_menu_tap`.

Signup (`EXEMPT_CONVERSATIONS`) is never ended by an exit, and while it is open
nothing is touched at all: it shares the phone keys with the profile phone flow.

An exit from a step that put a share-location or share-contact keyboard in place
of the bottom one (`_share_keyboard_up`) puts the bottom keyboard back, silently,
unless the gesture is /start or /menu, which install it themselves (spec §1).

The per-flow key tuples below are the ONLY lists of each flow's keys. The
flows' own cancels and timeouts import them, and only the exit pops their
union, so a flow's timeout can never drop another open flow's keys.

The customer-bot counterpart of `staff_bot/utils/flow_state.clear_pending_flows`.
A leaf module: `handlers.profile` and `handlers.subscriptions` import it, so it
imports `handlers` only lazily, inside `leave_all_flows`. `menu_keyboard`
imports it too, so `menu_keyboard` is imported only inside the functions that
use it.
"""
import logging
from typing import Optional, Union

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    filters,
)

from database import db_manager, BotUserRepository
from i18n import i18n
from keyboards import MAIN_MENU_KEYS
from menu_tap import resolve_installed_label, resolve_tapped_label

logger = logging.getLogger('bot')


FLOW_EXIT_GROUP = -3
EXIT_COMMANDS = ("start", "menu", "help", "language")
# The exit commands whose own answer installs the bottom keyboard, so the exit sends none.
SELF_INSTALLING_COMMANDS = ("start", "menu")
LEGACY_MENU_CALLBACK_PATTERN = (
    r"^(back_to_main|menu_(products|orders|subscriptions|loyalty|profile|support|language)|cart_view)$"
)
EXEMPT_CONVERSATIONS = frozenset({"registration"})
# The free text the group-0 catch-all (`bot.py::_handle_text_message`) answers. ONE object, used by
# that catch-all AND `FlowExitHandler`, because the router trusts `pending_menu_tap` alone to
# recognise a label: a text the catch-all gets must be one the exit could have resolved.
FREE_TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE

# `phone_otp` is deliberately absent: a code typed within its lifetime still
# verifies after the customer tapped elsewhere (spec §2, "Phone-code steps").
PROMPTS_LEFT_BY_EXIT = (
    "support_message",
    "edit_profile_name",
    "edit_profile_birthday",
    "edit_address_title",
    "edit_address_instructions",
)

# --- Each flow's `context.user_data` keys: the one list per flow -------------
ADDRESS_FLOW_KEYS = (
    'temp_location',
    'temp_address',
    'temp_address_data',
    'address_flow_origin',
    'awaiting_location',
    'awaiting_location_at',
)
# When the `PHONE_VERIFY_OTP` step's SMS went out. An exit carries it into the
# durable `phone_otp` prompt (`leave_all_flows`), so leaving never extends a code.
PHONE_OTP_SENT_AT = 'phone_otp_sent_at'
PHONE_FLOW_KEYS = (
    'pending_phone', 'pending_phone_verification', 'awaiting_otp', 'otp_prompted_update_id', PHONE_OTP_SENT_AT,
)
SUBSCRIPTION_CREATION_KEYS = ('subscription_creation', 'current_product_id')
ITEM_MANAGEMENT_KEYS = ('editing_subscription_id', 'adding_product_id')
UPDATE_ITEM_KEYS = ('editing_subscription_id', 'editing_item_id')
# Only the exit pops the union: a 300 s item flow timing out must not drop the
# basket a 600 s creation flow is still building.
SUBSCRIPTION_FLOW_KEYS = tuple(dict.fromkeys((*SUBSCRIPTION_CREATION_KEYS, *ITEM_MANAGEMENT_KEYS, *UPDATE_ITEM_KEYS)))
ADDRESS_EDIT_KEYS = ('edit_address_id', 'edit_field')

_MENU_TAP_PENDING = '_menu_tap_pending'
# `FlowExitHandler.check_update`'s answer for free text that is not a label as it reads now: it
# may still be one the customer has installed, which only `handle_update` can read (it is async).
_CHECK_INSTALLED_LABELS = object()

# `db_manager` is the shared instance, so the test harness's in-place method
# patches serve this repository too.
_user_repository = BotUserRepository(db_manager)


async def _no_op(_update, _context) -> None:
    return None


async def _end_flow(_update, _context):
    return ConversationHandler.END


class _EndFlowHandler(BaseHandler):
    """What `leave_all_flows` hands `ConversationHandler.handle_update` as "the
    handler that matched". It matches nothing on its own, so PTB never
    dispatches it, and it only answers END. No teardown of its own: those run
    once in `leave_all_flows`, open conversation or not."""

    __slots__ = ()

    def check_update(self, update: object) -> bool:
        return False


END_FLOW_HANDLER = _EndFlowHandler(_end_flow)
_SELF_INSTALLING = CommandHandler(SELF_INSTALLING_COMMANDS, _no_op, filters=filters.UpdateType.MESSAGE)


async def leave_all_flows(update, context) -> None:
    """End every unfinished flow for this chat except signup. Never raises.

    The order is load-bearing:

    1. Return, touching nothing, while signup is open
       (`exempt_conversation_active`); otherwise collect the open conversations.
    2. End each one through PTB's own `handle_update`, which pops and cancels
       its timeout job (so no "timed out" message arrives later) and then
       deletes the state.
    3. Disarm the saved prompts the exit leaves (`PROMPTS_LEFT_BY_EXIT`), then
       move an open `PHONE_VERIFY_OTP` code into the durable `phone_otp`
       prompt (`_keep_code_answerable`) while its keys are still in `user_data`.
    4. Run every flow's teardown, open or not: a restart empties the
       conversations but leaves `user_data` and `bot_state` behind.
    5. Put the bottom keyboard back when a share-location or share-contact
       keyboard was up (`_share_keyboard_up`, read before any teardown clears
       the pin prompt it reads), unless the gesture installs it itself.

    It never raises `ApplicationHandlerStop` and never re-dispatches: the
    update walks on to group 0, which draws the destination.
    """
    if update.effective_chat is None or update.effective_user is None:
        return
    if exempt_conversation_active(update, context.application):
        return  # signup is open: touch nothing, not even the phone keys it shares
    telegram_id = update.effective_user.id

    open_flows = []
    for handlers in context.application.handlers.values():
        for conv in handlers:
            if not isinstance(conv, ConversationHandler):
                continue
            key = conv._get_key(update)
            state = conv._conversations.get(key)
            if state is not None:
                open_flows.append((conv, key, state))
    share_keyboard_up = await _share_keyboard_up(context, telegram_id, open_flows)

    for conv, key, state in open_flows:
        try:
            await conv.handle_update(update, context.application, (state, key, END_FLOW_HANDLER, True), context)
        except Exception as error:
            logger.warning("Flow exit could not end %s for %s: %s", conv.name, telegram_id, error)
    if open_flows:
        logger.info("Flow exit for %s ended %s", telegram_id, [conv.name for conv, _key, _state in open_flows])

    try:
        await _user_repository.abandon_prompts(telegram_id, PROMPTS_LEFT_BY_EXIT)
    except Exception as error:
        logger.warning("Flow exit could not disarm the saved prompts for %s: %s", telegram_id, error)
    await _keep_code_answerable(context, telegram_id, open_flows)

    for name in (*PHONE_FLOW_KEYS, *SUBSCRIPTION_FLOW_KEYS, *ADDRESS_EDIT_KEYS):
        context.user_data.pop(name, None)
    try:
        from handlers.profile import profile_handlers

        await profile_handlers.clear_address_flow_keys(context, telegram_id)
    except Exception as error:
        logger.warning("Flow exit could not clear the address flow for %s: %s", telegram_id, error)

    if share_keyboard_up and _SELF_INSTALLING.check_update(update) in (None, False):
        await _put_the_menu_back(update, context, telegram_id)


async def _share_keyboard_up(context, telegram_id: int, open_flows) -> bool:
    """Is a share-contact or share-location keyboard on screen in place of the bottom one?

    * Profile -> Phone at `PHONE_VERIFY_PHONE` (share contact);
    * the address form at `ADDRESS_LOCATION` (share location). Its later steps
      sit behind the bottom keyboard already: the pin and Enter manually both
      send it, though Enter manually leaves the pin prompt armed;
    * no address form open, with a pin prompt armed (`utils.arm_location_request`,
      in `user_data` or its durable twin in bot_state): the zero-address
      checkout prompt, which starts no form, or a form a restart emptied.

    Signup never gets here (`leave_all_flows` returns first while it is open).
    A failed read answers False: the destination is still drawn.
    """
    try:
        from handlers.conversation_states import ADDRESS_LOCATION, PHONE_VERIFY_PHONE

        states = {conv.name: state for conv, _key, state in open_flows}
        if states.get("phone_verification") == PHONE_VERIFY_PHONE:
            return True
        if "address_conversation" in states:
            return states["address_conversation"] == ADDRESS_LOCATION
        if context.user_data.get('awaiting_location'):
            return True
        state = await _user_repository.get_user_state(telegram_id)
        return bool(state.get('awaiting_location_at'))
    except Exception as error:
        logger.warning("Flow exit could not read the share keyboard for %s: %s", telegram_id, error)
        return False


async def _put_the_menu_back(update, context, telegram_id: int) -> None:
    """Send the bottom keyboard, silently, through its one installer. `send_menu_keyboard`
    keeps its own gate: a chat without a phone gets the share keyboard taken down instead."""
    try:
        from menu_keyboard import send_menu_keyboard  # lazy: menu_keyboard imports this module

        language = await i18n.get_user_language(telegram_id)
        await send_menu_keyboard(
            context.bot, update.effective_chat.id, telegram_id,
            i18n.get('telegram.main_menu', language), language, silent=True,
        )
    except Exception as error:
        logger.warning("Flow exit could not put the menu back for %s: %s", telegram_id, error)


async def _keep_code_answerable(context, telegram_id: int, open_flows) -> None:
    """A customer leaving Profile -> Phone at its code step keeps that code answerable.

    `ProfileHandlers.keep_code_answerable` moves the step's code into the
    durable `phone_otp` prompt, stamped with the SMS's send time. It runs after
    `abandon_prompts`, so a prompt this exit leaves is never mistaken for a
    newer one, and before the `PHONE_FLOW_KEYS` pop, which it reads from.
    `phone_otp` is not in `PROMPTS_LEFT_BY_EXIT`, so the durable half stays.
    """
    try:
        from handlers.conversation_states import PHONE_VERIFY_OTP
        from handlers.profile import profile_handlers

        if any((conv.name, state) == ("phone_verification", PHONE_VERIFY_OTP) for conv, _key, state in open_flows):
            await profile_handlers.keep_code_answerable(context, telegram_id)
    except Exception as error:
        logger.warning("Flow exit could not keep the phone code answerable for %s: %s", telegram_id, error)


class FlowExitHandler(BaseHandler):
    """Claims every exit gesture and draws nothing; group 0 draws the destination.

    `check_update` answers True for an exit command or a legacy menu callback,
    and the `MAIN_MENU_KEYS` key for a typed label, which `handle_update`
    stashes for `pending_menu_tap` so the label is resolved once per update.

    Any other fresh text gets `_CHECK_INSTALLED_LABELS`: `handle_update` then
    reads the labels the customer still has installed (spec 2026-10-21 §1,
    Review Focus 4), so a button the owner retitled since it was sent keeps
    leaving the flow and opening its screen. A miss touches nothing, and while
    signup is open nothing is read at all: its stale labels stay signup's.
    """

    __slots__ = ("_commands", "_legacy_callbacks")

    def __init__(self):
        super().__init__(_no_op)
        self._commands = CommandHandler(EXIT_COMMANDS, _no_op, filters=filters.UpdateType.MESSAGE)
        self._legacy_callbacks = CallbackQueryHandler(_no_op, pattern=LEGACY_MENU_CALLBACK_PATTERN)

    def check_update(self, update: object) -> Optional[Union[bool, str]]:
        if not isinstance(update, Update):
            return None
        if self._commands.check_update(update) not in (None, False):
            return True
        if self._legacy_callbacks.check_update(update) not in (None, False):
            return True
        if FREE_TEXT_FILTER.check_update(update):
            return resolve_tapped_label(update.effective_message.text, MAIN_MENU_KEYS) or _CHECK_INSTALLED_LABELS
        return None

    async def handle_update(self, update, application, check_result, context) -> None:
        if check_result is _CHECK_INSTALLED_LABELS:
            if update.effective_user is None or exempt_conversation_active(update, application):
                return
            try:
                from menu_keyboard import installed_menu_labels  # lazy: this module stays a leaf

                installed = await installed_menu_labels(update.effective_user.id)
                check_result = resolve_installed_label(update.effective_message.text, installed)
            except Exception as error:
                logger.warning(
                    "Flow exit could not read the installed labels for %s: %s", update.effective_user.id, error
                )
                return
            if check_result is None:
                return  # not a label at all: the text belongs to whatever asked for it
        if isinstance(check_result, str) and context.user_data is not None:
            context.user_data[_MENU_TAP_PENDING] = (update.update_id, check_result)
        await leave_all_flows(update, context)


def exempt_conversation_active(update, application) -> bool:
    """Is a conversation in `EXEMPT_CONVERSATIONS` (signup) open for this chat?

    The one decider for "signup owns this customer": `leave_all_flows` touches
    nothing while it is True, the installed-label lookup reads nothing, and the
    lazy menu-keyboard installer (`menu_keyboard.ensure_menu_keyboard_middleware`)
    sends nothing. False for an update with no chat or user.
    """
    if update.effective_chat is None or update.effective_user is None:
        return False
    for handlers in application.handlers.values():
        for conv in handlers:
            if (
                isinstance(conv, ConversationHandler)
                and conv.name in EXEMPT_CONVERSATIONS
                and conv._conversations.get(conv._get_key(update)) is not None
            ):
                return True
    return False


def pending_menu_tap(update, context) -> Optional[str]:
    """The `MAIN_MENU_KEYS` key `FlowExitHandler` resolved for THIS update, or None.

    Popped either way, and keyed on the update id, so a label a conversation
    consumed before group 0 can never be answered by a later message.
    """
    user_data = context.user_data
    if user_data is None:
        return None
    pending = user_data.pop(_MENU_TAP_PENDING, None)
    if pending is None:
        return None
    update_id, key = pending
    return key if update_id == update.update_id else None
