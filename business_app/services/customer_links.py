"""Where a customer is sent back to an order: its page on the website, or its screen in the bot chat (spec
2026-10-27 §4.5).

A Pay tapped in the bot returns there after Click; a Pay tapped on the website returns to the order's page. The
channel comes with every tap (`POST /orders/<id>/retry-payment`), so a URL stored by an earlier tap never carries
over. A link built without a tap (the checkout's first link, the admin resend, a notice) takes the channel the order
came from.

Every URL is an f-string over the app config, never `url_for`: Celery builds these too, and it has no request
context. `website_url` is the one spelling of the website base and `bot_start_link` the one spelling of the bot's
`/start` link; every customer link built from either base goes through them (the reorder-reminder email, the
Telegram connect link), so a change to a base or its slash rule cannot fork.
"""

from typing import Tuple

from flask import current_app

from shared.constants import ORDER_START_PREFIX, SUBSCRIPTION_START_PREFIX

PAY_CHANNEL_BOT = "bot"
PAY_CHANNEL_WEB = "web"
PAY_CHANNELS = (PAY_CHANNEL_BOT, PAY_CHANNEL_WEB)


def channel_for_order(order) -> str:
    """The channel an order came from: the bot for a Telegram order, the website for any other."""
    return PAY_CHANNEL_BOT if order.order_source == "telegram" else PAY_CHANNEL_WEB


def website_url(path: str) -> str:
    """`path` (starting with "/") on the customer website: `COMPANY_WEBSITE` without a trailing slash, so a
    configured "https://aqua-element.uz/" never yields "//"."""
    return f"{current_app.config['COMPANY_WEBSITE'].rstrip('/')}{path}"


def bot_start_link(payload: str) -> str:
    """The bot chat's link that opens it with `/start <payload>` (`COMPANY_TELEGRAM_BOT_URL`, the one bot
    identity)."""
    return f"{current_app.config['COMPANY_TELEGRAM_BOT_URL']}?start={payload}"


def order_page_path(order) -> str:
    """The website page that shows an order: its subscription on /my-subscriptions for a subscription order,
    otherwise its tracking page."""
    if order.subscription_id:
        return f"/my-subscriptions?sub={order.subscription_id}"
    return f"/order-tracking?order_id={order.id}"


def order_page_url(order) -> str:
    return website_url(order_page_path(order))


def bot_order_link(order) -> str:
    """The bot chat's `/start` link to an order's screen: its subscription for a subscription order."""
    if order.subscription_id:
        return bot_start_link(f"{SUBSCRIPTION_START_PREFIX}{order.subscription_id}")
    return bot_start_link(f"{ORDER_START_PREFIX}{order.id}")


def pay_return_urls(order, channel: str) -> Tuple[str, str]:
    """Click's (success, cancel) URLs for a payment opened from `channel`: both lead back to where Pay was tapped,
    the order's screen in the bot chat or its page on the website."""
    if channel == PAY_CHANNEL_BOT:
        url = bot_order_link(order)
    elif channel == PAY_CHANNEL_WEB:
        url = order_page_url(order)
    else:
        raise ValueError(f"Unknown pay channel {channel!r}")
    return url, url
