"""The customer bot signs itself back in after the account's logins were ended (spec 2026-10-19 R9).

A password reset and the phone-link merge end every existing login of the
account, the bot's included: its refresh token stops refreshing, and its cached access token is
refused on its very next request. Neither may strand the customer. The bot drops the refused pair
and signs in again through the signed `/auth/telegram-login`, with no action from the customer.

Both journeys run through the REAL dispatcher and the real TokenManager (over an in-memory Redis):
* a refused refresh, with the backend answered by the harness double;
* the immediate cut-off, against the REAL backend: the bot's own `_make_request` (signing included)
  talks to the Flask app, and the password is reset through the storefront's real routes.
Only Telegram, the bot's own SQL, the reset e-mail task and the bot's token Redis are faked.
"""

from datetime import datetime, timezone

import pytest

import api_client as api_client_module
from token_manager import TokenManager

from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.test_session_revocation import _reset_password, next_second
from tests.integration.test_web_password_reset_and_change import emailed_reset_links  # noqa: F401  (a fixture)
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, _install_translations, backend_failure
from tests.telegram_bot.test_registration_journey_dispatcher import TRANSLATIONS, make_bot, texts  # noqa: F401
from tests.telegram_bot.test_token_manager import _FakeRedis, _jwt_with_exp

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ORDERS = "/api/v1/orders/"
TELEGRAM_LOGIN = "/api/v1/auth/telegram-login"
REFRESH = "/api/v1/auth/refresh"
NO_ORDERS = "You have no orders yet"

# A connected customer, as the bot's own users table holds them.
ROW = {
    "id": 4242,
    "telegram_id": str(DEFAULT_USER_ID),
    "first_name": "Kamola",
    "phone": "+998901112233",
    "preferred_language": "en",
    "role": "customer",
    "status": "active",
    "bot_state": "{}",
    "user_type": "individual",
}


async def _bot_with_token_manager(make_bot, monkeypatch, row):
    """The customer bot with the production TokenManager, attached the way bot.py attaches it."""
    bot = await make_bot(row=row)
    _install_translations(monkeypatch, {**TRANSLATIONS, "telegram.orders.no_orders": NO_ORDERS})
    tokens = TokenManager("redis://unused")
    tokens.redis, tokens._connected = _FakeRedis(), True
    bot.application.bot_data["token_manager"] = tokens
    monkeypatch.setattr(api_client_module.api_client, "token_manager", tokens)
    return bot, tokens


async def test_a_refused_refresh_signs_the_customer_in_again(make_bot, monkeypatch):
    bot, tokens = await _bot_with_token_manager(make_bot, monkeypatch, ROW)
    now = int(datetime.now(timezone.utc).timestamp())
    # A pair from before the reset: the access token is due for a refresh, the refresh token is not expired.
    await tokens.store_tokens(DEFAULT_USER_ID, _jwt_with_exp(now + 60), _jwt_with_exp(now + 86400))
    fresh = {"access_token": _jwt_with_exp(now + 3600), "refresh_token": _jwt_with_exp(now + 7200), "expires_in": 3600}
    bot.backend.route("POST", REFRESH, lambda _call: backend_failure("The token has been revoked.", 401))
    bot.backend.route("POST", TELEGRAM_LOGIN, lambda _call: {"data": fresh})
    bot.backend.route("GET", ORDERS, lambda _call: {"data": {"orders": []}})

    await bot.send(bot.updates().tap("menu_orders"))

    assert [(c.method, c.endpoint) for c in bot.backend.calls] == [
        ("POST", REFRESH), ("POST", TELEGRAM_LOGIN), ("GET", ORDERS)
    ]
    [login] = [c for c in bot.backend.calls if c.endpoint == TELEGRAM_LOGIN]
    assert (login.sign, login.data["telegram_id"]) == (True, DEFAULT_USER_ID)
    assert bot.backend.calls[-1].user_token == fresh["access_token"]
    assert (await tokens.get_cached_tokens(DEFAULT_USER_ID))["access_token"] == fresh["access_token"]
    assert texts(bot)[-1] == NO_ORDERS


# `db` before `make_bot`: anyio's runner task copies the context when the first async fixture is set
# up, so `db`'s app context (the one that scopes `db.session`) must already be pushed by then (C14).
async def test_after_a_password_reset_the_bot_signs_in_again_by_itself(
    app, db, sample_user, emailed_reset_links, make_bot, monkeypatch
):
    sample_user.telegram_id = str(DEFAULT_USER_ID)  # a customer connected to the bot
    db.session.commit()
    bot, tokens = await _bot_with_token_manager(make_bot, monkeypatch, {**ROW, "id": sample_user.id})
    backend = FlaskBackend(app)
    monkeypatch.delattr(api_client_module.api_client, "_make_request")  # the bot's own funnel, not the double
    monkeypatch.setattr(api_client_module.api_client, "_client", backend)
    user = bot.updates()
    await bot.send(user.tap("menu_orders"))  # the bot signs in once and caches the pair
    before = (await tokens.get_cached_tokens(DEFAULT_USER_ID))["access_token"]
    assert [step[:3] for step in backend.seen] == [("POST", TELEGRAM_LOGIN, 200), ("GET", ORDERS, 200)]
    next_second()

    _reset_password(app, sample_user, emailed_reset_links)
    backend.seen.clear()
    tokens.redis.locks.clear()  # the customer comes back later: the tap's 2 s dedup lock has expired
    await bot.send(user.tap("menu_orders"))

    after = (await tokens.get_cached_tokens(DEFAULT_USER_ID))["access_token"]
    assert [step[:3] for step in backend.seen] == [
        ("GET", ORDERS, 401),  # the cached token is refused at once
        ("POST", TELEGRAM_LOGIN, 200),  # the bot signs in again, signed
        ("GET", ORDERS, 200),  # and repeats the call with the new token
    ]
    assert backend.seen[-1][3] == f"Bearer {after}" and after != before
    assert texts(bot)[-1] == NO_ORDERS
