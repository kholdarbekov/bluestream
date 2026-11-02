"""The customer bot's Telegram client, which survives a missing BotFather domain.

A message that carries a `login_url` button ("Open on website") is refused by Telegram as a whole
(`BadRequest: BOT_DOMAIN_INVALID`) when BotFather's /setdomain is missing or names another domain. Without a
fallback the profile menu and every subscription card would stop showing on that environment. This is the one seam
every send and edit passes through (`_do_post`): on that exact refusal it drops the login buttons (and any row left
empty) and sends once more. Any other error propagates unchanged.
"""

import logging
from urllib.parse import urlparse

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ExtBot

logger = logging.getLogger(__name__)

# PTB capitalises the description it raises ("Bot_domain_invalid"), so compare upper-cased.
BOT_DOMAIN_INVALID = "BOT_DOMAIN_INVALID"


def _login_hosts(markup) -> list:
    """The hosts of the login buttons on `markup`; empty when it has none (or is not an inline keyboard)."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [
        urlparse(button.login_url.url).hostname
        for row in markup.inline_keyboard
        for button in row
        if button.login_url
    ]


def without_login_buttons(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """`markup` minus every `login_url` button and the rows that leaves empty."""
    rows = [[button for button in row if not button.login_url] for row in markup.inline_keyboard]
    return InlineKeyboardMarkup([row for row in rows if row])


class LoginSafeBot(ExtBot):
    """ExtBot that retries once, without login buttons, when Telegram refuses the bot's domain."""

    async def _do_post(self, endpoint, data, **kwargs):
        try:
            return await super()._do_post(endpoint, data, **kwargs)
        except BadRequest as error:
            hosts = _login_hosts(data.get("reply_markup"))
            if BOT_DOMAIN_INVALID not in str(error).upper() or not hosts:
                raise
            logger.warning("BotFather /setdomain missing for %s: sending %s without the login button", hosts[0], endpoint)
            retry = {**data, "reply_markup": without_login_buttons(data["reply_markup"])}
            return await super()._do_post(endpoint, retry, **kwargs)
