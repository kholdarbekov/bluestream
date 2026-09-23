"""The cart quantity screen's typing window: which screen a typed number answers.

The quantity screen (``ProductHandlers._render_quantity_step``) is not a
conversation — it is a plain inline keyboard whose only memory is the product
id baked into its buttons. A number TYPED at it carries no such id, so the bot
has to remember which screen is up. It does so durably, in
``users.bot_state['quantity_screen']`` (``BotUserRepository.
remember_quantity_screen``), because the customer's message can arrive after a
deploy and the whole point is that it no longer vanishes into the Support
Inbox.

Every render of the screen records the bubble showing it. The window closes on
anything else the customer does, so that a digit typed later can never move a
cart line they are no longer looking at:

* any tap that is not on the live screen's own bubble — including a tap on an
  OLDER quantity bubble for another product (if that tap redraws its screen,
  the render records it and the window moves there; if it does not — the
  "Quantity: N" label, a sold-out product, an API error — the window is gone
  rather than left aimed at the wrong product);
* a number swipe-replied to a bubble OLDER than the live screen (it answers
  that screen, whose product the window does not know), or forwarded (it is
  somebody else's number);
* any command;
* any message that is not a quantity — words, a phone number, a photo. Once
  the customer is talking to a person, a bare number that follows is most
  likely an answer to that person, and an operator's reply reaches the chat
  without any update this bot could see;
* ``QUANTITY_SCREEN_WINDOW_MINUTES`` after the screen was last drawn;
* logout / the inactive-user sweep, which wipe ``bot_state`` whole.

``leave_quantity_screen_middleware`` below is the one place that decides all of
this, rather than a disarm sprinkled into every handler a customer could reach.
So a live window means the quantity screen is the NEWEST thing the customer
touched — which is why the text router may consult it before any older armed
prompt.
"""

import logging
from typing import Any, Callable, Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes, filters

from database import BotUserRepository, db_manager
from quantity_input import parse_typed_quantity
from utils import is_stamp_stale

logger = logging.getLogger('handlers')

# Same window as "Report an issue" and the pin prompt: long enough to think
# about how much water to order, short enough that a number typed after lunch
# is a message to a person, not a cart edit.
QUANTITY_SCREEN_WINDOW_MINUTES = 30

# The quantity screen's own buttons (and "Add to cart", which opens one). A
# tap on one of them keeps the window only when it lands on the live bubble.
_SCREEN_CALLBACK_PREFIXES = ('qty_', 'add_to_cart_')

_user_repository = BotUserRepository(db_manager)


def live_quantity_screen(user_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The quantity screen a typed number would answer, or None.

    None when no screen was recorded, the window expired, or the record is
    unreadable — every one of which means "this message is not a quantity".
    """
    screen = (user_state or {}).get('quantity_screen')
    if not isinstance(screen, dict):
        return None
    if is_stamp_stale(screen.get('shown_at'), QUANTITY_SCREEN_WINDOW_MINUTES):
        return None
    try:
        product_id = int(screen.get('product_id'))
    except (TypeError, ValueError):
        return None
    return {**screen, 'product_id': product_id}


# Given the recorded screen, does it survive this update?
_Keeps = Callable[[Dict[str, Any]], bool]


def _leaves_always(_screen: Dict[str, Any]) -> bool:
    return False


def _keeps_the_window(update: Update) -> Optional[_Keeps]:
    """Whether a recorded screen survives this update, or None when the update
    cannot touch it (so no database read is needed).

    Only a tap ON the live bubble, or a number typed at it, keeps it.
    """
    query = update.callback_query
    if query is not None:
        if (query.data or '').startswith(_SCREEN_CALLBACK_PREFIXES) and query.message is not None:
            tapped = query.message.message_id
            return lambda screen: screen.get('message_id') == tapped
        return _leaves_always

    message = update.message
    if message is None:
        # Edits, payments' pre-checkout queries, and the like: not the
        # customer moving anywhere. An edited "7" is ignored bot-wide.
        return None
    if filters.COMMAND.check_update(update) or message.forward_origin is not None:
        return _leaves_always
    if parse_typed_quantity(message.text or '') is None:
        return _leaves_always
    if message.reply_to_message is not None:
        # Message ids only grow within a chat: a reply to anything older than
        # the live screen is answering some other screen. A reply to the live
        # screen, or to the refusal sent under it, is answering this one.
        replied_to = message.reply_to_message.message_id
        return lambda screen: replied_to >= (screen.get('message_id') or 0)
    return None


async def leave_quantity_screen_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close the typing window when the customer does anything but use the screen.

    Registered as a ``TypeHandler`` in its own group, after the callback-dedup
    guard (a dropped duplicate never gets here) and before every conversation
    and handler, so the window is already closed by the time whatever the
    customer did arms its own prompt. Never stops dispatch, and a failure here
    only logs: the 30-minute expiry is the backstop.
    """
    user = update.effective_user
    if user is None:
        return
    keeps = _keeps_the_window(update)
    if keeps is None:
        return
    try:
        state = await _user_repository.get_user_state(user.id)
        screen = state.get('quantity_screen')
        if screen is not None and not (isinstance(screen, dict) and keeps(screen)):
            await _user_repository.forget_quantity_screen(user.id)
    except Exception as exc:
        logger.warning("Could not close the quantity screen's typing window for %s: %s", user.id, exc)
