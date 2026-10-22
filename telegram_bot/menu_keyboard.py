"""The persistent bottom keyboard: its labels, its one sender and its install marker.

`send_menu_keyboard` is the ONLY place that installs the keyboard (spec 2026-10-21 §1). It
writes the `menu_keyboard` marker in bot_state only after Telegram accepted the send, so the
marker always describes a keyboard that is really on the customer's screen.

`ensure_menu_keyboard_middleware` installs it lazily for customers who already have the bot
(spec §1, "Existing customers, no broadcast"): on their first inline tap, or the first one after
the keyboard they have stopped matching what would render now.
"""

import logging
from typing import Mapping, Optional

from telegram import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import TelegramError

import eligibility
from database import BotUserRepository, db_manager
from flow_exit import exempt_conversation_active
from i18n import i18n
from keyboards import MAIN_MENU_KEYS

logger = logging.getLogger('bot')

MENU_KEYBOARD_STATE_KEY = "menu_keyboard"
# PTB handler group of the lazy installer (`ensure_menu_keyboard_middleware`), defined here beside
# the marker it keeps true: after the reachability recovery (-9), before the dedup guard (-5).
MENU_INSTALLER_GROUP = -6
_LOYALTY_KEY = "telegram.menu.loyalty"


def wanted_menu_labels(language: str, show_loyalty: bool = True) -> dict[str, str]:
    """The labels the keyboard shows, by translation key, in layout order. The only place that
    decides which keys render."""
    return {
        key: i18n.get(key, language)
        for key in MAIN_MENU_KEYS
        if show_loyalty or key != _LOYALTY_KEY
    }


async def installed_menu_labels(telegram_id: int) -> dict[str, str]:
    """The labels of the keyboard recorded as installed, or {} when none is."""
    state = await BotUserRepository(db_manager).get_user_state(telegram_id)
    return (state.get(MENU_KEYBOARD_STATE_KEY) or {}).get('labels', {})


def is_menu_customer(row: Optional[Mapping]) -> bool:
    """The one rule for "may have the keyboard": the row exists and has a phone."""
    return bool(row and row.get('phone'))


def assert_editable_markup(reply_markup) -> None:
    """An edit can only carry inline buttons; Telegram answers 400 to anything else."""
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        raise TypeError(
            "an edit can only carry inline buttons; "
            "send the bottom keyboard with menu_keyboard.send_menu_keyboard"
        )


async def send_menu_keyboard(
    bot, chat_id: int, telegram_id: int, text: str, language: str,
    *, silent: bool = False, parse_mode: Optional[str] = None,
) -> Optional[Message]:
    """Send `text` with the bottom keyboard and record what was installed.

    A chat that has not finished signing up gets the text with the keyboard taken down instead,
    and no marker. Returns None when Telegram refused the send.
    """
    repo = BotUserRepository(db_manager)
    row = await repo.get_user_by_telegram_id(telegram_id)
    customer = is_menu_customer(row)
    if customer:
        markup = await eligibility.main_menu_for(telegram_id, language)
    else:
        markup = ReplyKeyboardRemove()

    kwargs = {}
    if silent:
        kwargs['disable_notification'] = True
    try:
        message = await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode=parse_mode, **kwargs
        )
    except TelegramError as exc:
        logger.warning("Could not send the menu keyboard to %s: %s", telegram_id, exc)
        return None

    if customer:
        sent = {button.text for keyboard_row in markup.keyboard for button in keyboard_row}
        labels = {
            key: label for key, label in wanted_menu_labels(language, show_loyalty=True).items()
            if label in sent
        }
        marker = {"labels": labels, "language": language, "loyalty": _LOYALTY_KEY in labels}
        try:
            await repo.remember_menu_keyboard(telegram_id, marker)
        except Exception:
            logger.exception("Menu keyboard sent to %s but its marker was not saved", telegram_id)
    return message


async def ensure_menu_keyboard_middleware(update, context) -> None:
    """Install the bottom keyboard, silently, for a customer whose screen lacks the current one.

    Registered as a `TypeHandler` at `MENU_INSTALLER_GROUP`, so it runs BEFORE dispatch: the
    keyboard goes out ahead of the screen the tap opens, and a phone or location keyboard that
    screen sends afterwards still wins. Inline taps only: typed text is never answered with a
    keyboard. Skipped while signup owns the chat, for a chat that has not finished signing up,
    and when the installed keyboard already matches what would render now (labels, language,
    Aqua Club). Never stops dispatch and never raises.
    """
    try:
        if update.callback_query is None or update.effective_chat is None or update.effective_user is None:
            return
        if exempt_conversation_active(update, context.application):
            return
        telegram_id = update.effective_user.id
        row = await BotUserRepository(db_manager).get_user_by_telegram_id(telegram_id)
        if not is_menu_customer(row):
            return
        # `i18n.get_user_language`'s own rule on the same column, without a second query per tap.
        language = i18n.normalize_language(row.get('preferred_language'))
        loyalty = await eligibility.is_loyalty_eligible(telegram_id)
        if await installed_menu_labels(telegram_id) == wanted_menu_labels(language, loyalty):
            return
        await send_menu_keyboard(
            context.bot, update.effective_chat.id, telegram_id,
            i18n.get('telegram.main_menu', language), language, silent=True,
        )
    except Exception:
        logger.exception("Lazy menu-keyboard install failed")
