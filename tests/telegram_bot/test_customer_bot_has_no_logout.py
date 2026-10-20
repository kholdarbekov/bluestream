"""The customer bot has no Logout (spec 2026-10-19 R10, owner decision 2026-10-20).

Each app logs out only itself, and a Telegram bot has nothing to log out of: the chat is the
login. Profile -> Logout and the "log out of all devices" call behind it are gone. A button from
a message sent before this change (none was ever wired in bot.py) would find no handler: PTB
ignores the tap, so it neither fails nor changes anything.
"""

import pytest

from api_client import BusinessAPIClient
from database import BotUserRepository
from handlers.profile import ProfileHandlers
from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# The two callback_data values the removed handlers owned: the Profile menu's Logout button was
# never wired in bot.py, but its confirm button was posted by `logout_handler`.
RETIRED_TAPS = ["confirm_logout", "profile_logout", "logout"]


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


def test_the_profile_handlers_no_longer_offer_a_logout():
    assert not hasattr(ProfileHandlers, "logout_handler")
    assert not hasattr(ProfileHandlers, "confirm_logout")


def test_the_api_client_has_no_log_out_of_all_sessions_call():
    assert not hasattr(BusinessAPIClient, "logout_all_sessions")


def test_nothing_clears_a_bot_users_state_for_a_logout_any_more():
    assert not hasattr(BotUserRepository, "clear_user_session")


@pytest.mark.parametrize("data", RETIRED_TAPS)
async def test_a_tap_on_an_old_logout_button_is_finds_no_handler_and_fails_nothing(bot, user, data):
    errors = []
    bot.application.add_error_handler(lambda update, context: errors.append(context.error) or None)

    assert bot.handlers_matching(user.tap(data)) == []  # no handler claims it, so none can fail
    await bot.send(user.tap(data))

    assert errors == []
