"""The customer bot's "Open on website" buttons (spec 2026-10-27 §9, step 4 Task 16b).

A Telegram `login_url` button on the profile menu and on a subscription card signs the customer in on the website at
`/my-account` or at that subscription. Telegram refuses a login button whose host is not a public https site, and one
refused button fails the whole menu, so the buttons exist only then. Driven through the real PTB dispatcher.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from keyboards import ProfileKeyboards, SubscriptionKeyboards
from shared.telegram_login import is_allowed_login_target
from tests.telegram_bot.ptb_harness import FakeDatabase, build_bot_harness, menu_already_installed
from tests.telegram_bot.test_order_customer_actions_dispatcher import TRANSLATIONS, _copy, tap
from tests.telegram_bot.web_fixtures import serve

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SITE = "https://aqua-element.uz"
PROFILE = "/api/v1/auth/profile"
OPEN_ON_WEBSITE = _copy("telegram.common.open_on_website", "uz")  # the harness customer's language is uz


def _login_buttons(bot) -> list[dict]:
    shown = bot.telegram.last_shown()
    assert shown.refused is None, shown.refused
    return [
        button
        for row in shown.reply_markup.get("inline_keyboard") or []
        for button in row
        if "login_url" in button
    ]


async def _profile_bot(monkeypatch, site: str):
    monkeypatch.setenv("COMPANY_WEBSITE", site)
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=FakeDatabase())
    bot.user = bot.updates()
    bot.backend.route("GET", PROFILE, lambda _c: {"data": {"id": 1, "first_name": "Aziz", "phone": "+998901234567"}})
    return bot


async def _card_bot(monkeypatch, site: str):
    monkeypatch.setenv("COMPANY_WEBSITE", site)
    database = FakeDatabase()
    database.user["preferred_language"] = "uz"
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database)
    bot.user = bot.updates()
    await menu_already_installed(bot)
    serve(bot, "needs_payment")
    await tap(bot, "subscription_1")
    return bot


async def test_the_profile_offers_open_on_website_as_a_telegram_login_button(monkeypatch):
    bot = await _profile_bot(monkeypatch, SITE)

    await tap(bot, "menu_profile")

    assert _login_buttons(bot) == [
        {"text": OPEN_ON_WEBSITE, "login_url": {"url": f"{SITE}/auth/telegram-login?next=%2Fmy-account"}}
    ]


async def test_the_subscription_card_opens_that_subscription_on_the_website(monkeypatch):
    bot = await _card_bot(monkeypatch, SITE)

    buttons = _login_buttons(bot)

    assert buttons == [
        {"text": OPEN_ON_WEBSITE, "login_url": {"url": f"{SITE}/auth/telegram-login?next=%2Fmy-subscriptions%3Fsub%3D1"}}
    ]
    # Above Back, the card's last button.
    rows = bot.telegram.last_shown().reply_markup["inline_keyboard"]
    assert "login_url" in rows[-2][0] and rows[-1][0]["callback_data"] == "back_to_subscriptions"


@pytest.mark.parametrize("site", ["http://localhost:5000", "http://aqua-element.uz", "https://business_app"])
async def test_no_login_button_without_a_public_https_site(monkeypatch, site):
    profile = await _profile_bot(monkeypatch, site)
    await tap(profile, "menu_profile")
    card = await _card_bot(monkeypatch, site)

    assert _login_buttons(profile) == []
    assert _login_buttons(card) == []
    assert profile.telegram.last_shown().button_labels()  # the menu itself still renders


def test_every_target_the_bot_builds_is_one_the_site_accepts(monkeypatch):
    """Harvest what the keyboards emit, parse each `next`, and hold it to the website's own allowlist."""
    monkeypatch.setenv("COMPANY_WEBSITE", SITE)
    markups = [ProfileKeyboards.profile_menu("en"), SubscriptionKeyboards.subscription_actions(7, None, "en")]

    urls = [
        button.login_url.url
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
        if button.login_url
    ]

    targets = [parse_qs(urlparse(url).query)["next"][0] for url in urls]
    assert targets == ["/my-account", "/my-subscriptions?sub=7"]
    assert all(is_allowed_login_target(target) for target in targets)


# --------------------------------------------------------------------------- BotFather /setdomain missing


def _refuse_login_buttons(bot, description: str) -> None:
    """Telegram refuses any message whose keyboard carries a login button (the domain is not BotFather's)."""
    for method in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
        bot.telegram.failures[method] = lambda params, method=method: _answer_unless_login(bot, method, params, description)


def _answer_unless_login(bot, method: str, params: dict, description: str):
    if "login_url" in str(params.get("reply_markup")):
        return 400, {"ok": False, "error_code": 400, "description": description}
    return 200, {"ok": True, "result": bot.telegram._result_for(method, params)}


DOMAIN_INVALID = "Bad Request: BOT_DOMAIN_INVALID"


async def test_a_missing_botfather_domain_still_shows_the_profile_menu_without_the_login_row(monkeypatch, caplog):
    bot = await _profile_bot(monkeypatch, SITE)
    _refuse_login_buttons(bot, DOMAIN_INVALID)

    with caplog.at_level("WARNING", logger="login_safe_bot"):
        await tap(bot, "menu_profile")

    arrived = bot.telegram.last_shown()
    assert arrived.refused is None
    assert "login_url" not in str(arrived.reply_markup)
    assert "edit_profile" in arrived.callback_data()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "setdomain" in r.getMessage()]
    assert len(warnings) == 1 and "BotFather /setdomain missing for aqua-element.uz" in warnings[0]


async def test_a_missing_botfather_domain_still_shows_the_subscription_card_without_the_login_row(monkeypatch):
    monkeypatch.setenv("COMPANY_WEBSITE", SITE)
    database = FakeDatabase()
    database.user["preferred_language"] = "uz"
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database)
    bot.user = bot.updates()
    await menu_already_installed(bot)
    serve(bot, "needs_payment")
    _refuse_login_buttons(bot, DOMAIN_INVALID)

    await tap(bot, "subscription_1")

    arrived = bot.telegram.last_shown()
    assert arrived.refused is None
    assert "login_url" not in str(arrived.reply_markup)
    assert arrived.callback_data()[-1] == "back_to_subscriptions"


async def test_another_bad_request_is_not_retried(monkeypatch):
    bot = await _profile_bot(monkeypatch, SITE)
    _refuse_login_buttons(bot, "Bad Request: chat not found")

    await tap(bot, "menu_profile")

    edits = [c for c in bot.telegram.calls if c.method == "editMessageText"]
    assert len(edits) == 1, "the refused edit was attempted once and not re-sent without its login button"
    assert "login_url" in str(edits[0].reply_markup) and edits[0].refused == "Bad Request: chat not found"
