"""Drive the customer bot the way Telegram drives it.

WHY THIS EXISTS
---------------
Every other test in ``tests/telegram_bot/`` calls a handler coroutine directly
with a hand-rolled ``DummyUpdate``. That proves a handler does the right thing
when it is called — it cannot prove the handler is ever CALLED. The whole
wiring layer is invisible to it:

* whether a keyboard's ``callback_data`` matches any registered pattern,
* whether that pattern is registered in the conversation STATE the customer is
  actually parked in,
* what a second handler group does with the same update,
* what happens when Telegram answers a real API call with a real error.

Those are precisely the seams the production defects lived in. So this harness
builds the REAL :class:`telegram.ext.Application` from the real
``WaterBusinessBot._setup_handlers()``, and feeds it real
:class:`telegram.Update` objects through ``process_update``.

THE THREE SEAMS
---------------
Only genuine I/O is faked, and each kind exactly once:

1. :class:`FakeTelegramTransport` — a real ``telegram.request.BaseRequest``.
   Every ``Bot`` method above it is the real one, so ``send_message`` really
   serialises its keyboard and really parses the reply into a ``Message``, and
   a scripted 400 really raises :class:`telegram.error.BadRequest`.
2. ``api_client._make_request`` — the single funnel every backend call passes
   through, so the real ``add_user_address`` / ``get_cart`` / … wrappers and
   their real endpoint paths are exercised.
3. ``db_manager`` — the single funnel for the bot's own SQL.

Everything between those seams is production code.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from telegram import Update
from telegram.ext import Application, ConversationHandler

# Bot modules resolve by bare name; tests/telegram_bot/conftest.py puts
# telegram_bot/ first on sys.path.
import i18n as i18n_module


from tests.bot_dispatcher_harness import (  # noqa: F401  (re-exported)
    _is_catch_all,
    DEFAULT_CHAT_ID,
    DEFAULT_USER_ID,
    FakeTelegramTransport,
    TelegramCall,
    UpdateFactory,
)


# ---------------------------------------------------------------------------
# Seam 2 — the backend
# ---------------------------------------------------------------------------


@dataclass
class BackendCall:
    method: str
    endpoint: str
    data: Optional[dict] = None
    params: Optional[dict] = None


class FakeBackend:
    """Routes ``api_client._make_request`` against an in-memory backend.

    Patched at ``_make_request`` rather than at the wrapper methods so the real
    ``add_user_address`` / ``create_order`` / … keep running: their endpoint
    paths and payload shapes are part of what these tests are checking.
    """

    def __init__(self):
        self.calls: list[BackendCall] = []
        self.addresses: dict[int, dict] = {}
        self.routes: dict[tuple[str, str], Callable[[BackendCall], Any]] = {}
        self._next_address_id = 900

    # -- routing --------------------------------------------------------------

    def route(self, method: str, endpoint: str, responder):
        """Override or add one endpoint. ``responder`` returns the JSON body."""
        self.routes[(method.upper(), endpoint)] = responder

    async def handle(self, method, endpoint, data=None, params=None, **_kwargs):
        call = BackendCall(method.upper(), endpoint, data, params)
        self.calls.append(call)

        responder = self.routes.get((call.method, endpoint))
        if responder is not None:
            body = responder(call)
        else:
            body = self._default(call)

        if isinstance(body, _Failure):
            return _api_response(False, error=body.error, status_code=body.status_code)
        return _api_response(True, data=body)

    def _default(self, call: BackendCall) -> Any:
        method, endpoint = call.method, call.endpoint

        if endpoint == "/api/v1/auth/telegram-login":
            return {
                "data": {
                    "access_token": "test-access-token",
                    "refresh_token": "test-refresh-token",
                    "user": {"id": 398, "telegram_id": DEFAULT_USER_ID},
                }
            }
        if endpoint == "/api/v1/addresses/reverse-geocode":
            return {"data": {"formatted_address": "15, Chilonzor dahasi, Toshkent shahri"}}
        if endpoint == "/api/v1/addresses/geocode":
            return {
                "data": {
                    "latitude": 41.2876,
                    "longitude": 69.2224,
                    "formatted_address": "15, Chilonzor dahasi, Toshkent shahri",
                }
            }
        if endpoint == "/api/v1/auth/addresses":
            if method == "GET":
                return {"data": {"addresses": list(self.addresses.values())}}
            if method == "POST":
                address_id = self._next_address_id
                self._next_address_id += 1
                row = {"id": address_id, "is_default": not self.addresses, **(call.data or {})}
                self.addresses[address_id] = row
                return {"data": {"address": row}}
        if endpoint.startswith("/api/v1/auth/addresses/"):
            address_id = int(endpoint.rsplit("/", 1)[-1])
            if method in {"PUT", "PATCH"}:
                self.addresses.setdefault(address_id, {"id": address_id}).update(call.data or {})
                return {"data": {"address": self.addresses[address_id]}}
            if method == "DELETE":
                self.addresses.pop(address_id, None)
                return {"data": {}}
        if endpoint == "/api/v1/cart":
            return {"data": {"items": [], "total_amount": 0}}
        if endpoint == "/api/v1/support/messages":
            return {"data": {"id": 1}}

        return {"data": {}}


@dataclass
class _Failure:
    error: str
    status_code: int = 500


def backend_failure(error: str, status_code: int = 500) -> _Failure:
    """Return this from a :meth:`FakeBackend.route` responder to fail the call."""
    return _Failure(error=error, status_code=status_code)


def _api_response(success, data=None, error=None, status_code=200):
    from api_client import APIResponse

    return APIResponse(success=success, data=data, error=error, status_code=status_code)


# ---------------------------------------------------------------------------
# Seam 3 — the bot's own SQL
# ---------------------------------------------------------------------------


@dataclass
class FakeDatabase:
    """Serves the handful of queries the bot issues directly.

    Keyed on a distinctive fragment of each query rather than on the full SQL so
    whitespace changes do not silently turn a row into ``None`` — which in this
    codebase means "unknown user", and unknown users get bounced to /start.
    """

    user: dict = field(
        default_factory=lambda: {
            "id": 398,
            "telegram_id": str(DEFAULT_USER_ID),
            "first_name": "Kamola",
            "phone": "+998978730111",
            "preferred_language": "uz",
            "role": "customer",
            "status": "active",
            "bot_state": "{}",
            "user_type": "individual",
        }
    )
    loyalty_eligible: bool = True
    executed: list[str] = field(default_factory=list)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def execute(self, query, *args):
        self.executed.append(query)
        if "bot_state" in query and args:
            self.user["bot_state"] = args[0]
        return "UPDATE 1"

    async def fetchone(self, query, *args):
        if "FROM users" in query:
            return dict(self.user)
        return None

    async def fetchall(self, query, *args):
        if "FROM translations" in query:
            return []
        return []

    async def fetchval(self, query, *args):
        if "preferred_language" in query:
            return self.user.get("preferred_language")
        if "loyalty" in query.lower():
            return self.loyalty_eligible
        if "bot_state" in query:
            # `BotUserRepository.get_user_state` reads this column through
            # `fetchval`; `execute` above already writes it on every
            # `update_user_state` call. Wired to the same dict so a test that
            # arms a flow (concern report, OTP, address-title prompt, ...)
            # and then sends a follow-up update sees the state it just wrote,
            # instead of every customer silently looking permanently unarmed.
            return self.user.get("bot_state")
        return None


# ---------------------------------------------------------------------------
# Assembling the application
# ---------------------------------------------------------------------------


class FakeTokenManager:
    """Enough of TokenManager for the dedup middleware and get_auth_token."""

    def __init__(self):
        self.redis = None
        self.tokens: dict[int, str] = {}

    async def get_valid_token(self, user_id, *_args, **_kwargs):
        return self.tokens.setdefault(user_id, "test-access-token")

    async def store_tokens(self, user_id, *_args, **_kwargs):
        self.tokens[user_id] = "test-access-token"
        return True

    async def invalidate_tokens(self, user_id):
        self.tokens.pop(user_id, None)
        return True


@dataclass
class BotHarness:
    """One assembled customer bot plus the three seams, ready to be driven."""

    application: Application
    telegram: FakeTelegramTransport
    backend: FakeBackend
    database: FakeDatabase

    def updates(self, **kwargs) -> "UpdateFactory":
        """An update factory bound to this harness's bot.

        Binding matters: PTB's `Message.reply_text` and friends resolve the bot
        off the object itself, so an Update built with `de_json(..., None)`
        raises "This object has no bot associated with it" the moment a handler
        uses a shortcut — which every handler here does.
        """
        return UpdateFactory(bot=self.application.bot, **kwargs)

    async def send(self, update: Update):
        """Deliver one update exactly as the poller would."""
        await self.application.process_update(update)
        return update

    def conversation_state(self, name: str, chat_id=DEFAULT_CHAT_ID, user_id=DEFAULT_USER_ID):
        """The state the named ConversationHandler has this customer parked in.

        ``None`` means "not in that conversation" — which is what an escaped
        tap or a silently ended flow looks like from the customer's side.
        """
        handler = self.conversation(name)
        return handler._conversations.get((chat_id, user_id))

    def conversation(self, name: str) -> ConversationHandler:
        for group in self.application.handlers.values():
            for handler in group:
                if isinstance(handler, ConversationHandler) and handler.name == name:
                    return handler
        raise AssertionError(f"no ConversationHandler named {name!r} is registered")

    def handlers_matching(self, update: Update, include_catch_alls: bool = False) -> list:
        """Every registered handler, in dispatch order, that claims this update.

        Empty means the tap lands nowhere — the failure mode that shows the
        customer a spinner and then nothing.

        CATCH-ALLS ARE EXCLUDED BY DEFAULT, and that is what makes this useful
        as evidence. Three registered handlers match literally every update: the
        two `TypeHandler(Update, ...)` middlewares (the debug logger at group
        -10 and the callback-dedup guard at -5) and the pattern-less
        `CallbackQueryHandler(debug_callback_handler)` at -1. None of them
        PROCESSES anything. Counting them would make "is this button wired?"
        answer yes for every string, including a button that does not exist —
        which is exactly how a wiring test goes quietly vacuous. Pass
        `include_catch_alls=True` only to assert on the middlewares themselves.
        """
        matched = []
        for group in sorted(self.application.handlers):
            for handler in self.application.handlers[group]:
                if handler.check_update(update) in (None, False):
                    continue
                if not include_catch_alls and _is_catch_all(handler):
                    continue
                matched.append((group, handler))
        return matched


async def build_bot_harness(monkeypatch, *, translations=None, database=None) -> BotHarness:
    """Assemble the real customer bot against the three fake seams."""
    import api_client as api_client_module
    import database as database_module
    from telegram.ext import ApplicationBuilder

    telegram = FakeTelegramTransport()
    backend = FakeBackend()
    db = database or FakeDatabase()

    # Seam 3: the bot's own SQL. Patched METHOD BY METHOD on the real
    # `db_manager` instance rather than by rebinding the name: handler
    # singletons are constructed at import time and each captured the real
    # object inside its own `BotUserRepository`, so rebinding the module
    # attribute would leave `profile_handlers.user_repo.db` pointing at the
    # real (unconnected) manager — "RuntimeError: Database not connected".
    for method in ("connect", "disconnect", "execute", "fetchone", "fetchall", "fetchval"):
        monkeypatch.setattr(database_module.db_manager, method, getattr(db, method))

    # Seam 2: the backend, at the single funnel every wrapper method uses.
    monkeypatch.setattr(api_client_module.api_client, "_make_request", backend.handle)
    monkeypatch.setattr(api_client_module.api_client, "start", _noop)
    monkeypatch.setattr(api_client_module.api_client, "aclose", _noop)

    _install_translations(monkeypatch, translations)

    # Importing `bot` runs setup_logging(); tests/telegram_bot/conftest.py's
    # autouse fixture restores log propagation so caplog keeps working.
    from bot import WaterBusinessBot

    application = (
        ApplicationBuilder()
        .token("424242:TEST-TOKEN")
        .request(telegram)
        .get_updates_request(telegram)
        .build()
    )

    water_bot = WaterBusinessBot()
    water_bot.application = application
    water_bot.token_manager = FakeTokenManager()
    application.bot_data["token_manager"] = water_bot.token_manager

    await water_bot._setup_handlers()
    await application.initialize()

    # user_middleware memoises across tests through a module-level cache, and
    # every test here uses the same DEFAULT_USER_ID, so one test's customer row
    # would serve another test's user_middleware for the next 300 seconds.
    #
    # Reach into `.cache` deliberately: `UserCache` (telegram_bot/utils.py:190)
    # exposes get/set/remove and NO `clear`, so the obvious
    # `getattr(user_cache, "clear", lambda: None)()` is a silent no-op that
    # merely looks like a reset — which is what it was here until 2026-08-21.
    import utils as utils_module

    utils_module.user_cache.cache.clear()

    # The callback-dedup middleware falls back to a MODULE-LEVEL dict when
    # Redis is absent, keyed on (user_id, callback_data) for 2 seconds. Two
    # tests tapping the same button inside one pytest process would otherwise
    # have the second tap silently dropped before any handler ran — a green
    # suite hiding a test that never executed.
    from handlers import callback_dedup

    callback_dedup._in_memory_locks.clear()

    # `rate_limiter` connects to whatever `config.redis.url` says. TODAY that
    # already lands on the test-isolated DB because `USE_SECRETS_FALLBACK`
    # defaults on (unset here — this harness runs via plain `docker run`, not
    # `docker compose`, so docker-compose.yml's explicit `USE_SECRETS_FALLBACK=true`
    # never even applies) and `config.py`'s local fallback `get_redis_url()`
    # checks the `REDIS_URL` env var first, which `scripts/precommit-backend-tests.sh`
    # points at DB 15. But that is incidental, not guaranteed: the moment
    # `USE_SECRETS_FALLBACK=false`, `shared.secrets_manager.get_redis_url()`
    # takes over instead, and it ignores `REDIS_URL` entirely — it rebuilds
    # the URL from `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB`, and `.env` sets
    # `REDIS_DB=0`, the DEV STACK'S REAL DATABASE. Pin the actual attribute
    # `RateLimiter`/`OTPRateLimiter` read so isolation holds no matter which
    # of the two `get_redis_url` implementations resolves at import time.
    import config as config_module

    _test_redis_url = os.environ.get('REDIS_URL')
    if _test_redis_url:
        config_module.config.redis.url = _test_redis_url

    # `rate_limiter`/`otp_rate_limiter` are module-level singletons that cache
    # their `redis.asyncio` connection on `._redis` plus a sticky `._redis_available`
    # flag. Each test function gets its own event loop, so a connection opened
    # under a PREVIOUS test's loop fails every pipeline call under this one with
    # "Event loop is closed" — and because `._redis_available` was left True, the
    # ensure-connected check short-circuits straight past reconnecting, straight
    # into that broken pipeline. Fail-closed then denies the request outright, so
    # any handler gated by `rate_limiter.allow_request` (e.g. `_handle_text_message`,
    # `_handle_attachment_message`) silently no-ops for every test after the first
    # one in a file. Force a fresh connection attempt per test.
    utils_module.rate_limiter._redis = None
    utils_module.rate_limiter._redis_available = False
    utils_module.rate_limiter._last_connect_attempt = None
    utils_module.otp_rate_limiter._redis = None
    utils_module.otp_rate_limiter._redis_available = False
    utils_module.otp_rate_limiter._last_connect_attempt = None

    telegram.reset()
    return BotHarness(application, telegram, backend, db)


async def _noop(*_args, **_kwargs):
    return None


def _patch_bot_modules(monkeypatch, attribute: str, value):
    """Rebind ``attribute`` in every already-imported bot module that holds it.

    ``from database import db_manager`` copies the reference into each module's
    globals, so patching only the defining module leaves every importer bound
    to the real object.
    """
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None) or ""
        if "/telegram_bot/" not in origin:
            continue
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, value, raising=False)


def _install_translations(monkeypatch, translations):
    """Serve translations from memory instead of the database.

    Only the LOOKUP is faked. The RENDERING is production's own
    ``shared.i18n_rendering.render_translation``, deliberately.

    This stub used to re-implement the rendering rule ("format when the caller
    passed values, otherwise hand back the raw template"), and that hand-rolled
    copy went stale the day production changed: `get()` stopped returning
    unfilled templates, five live call sites broke, and every dispatcher journey
    here kept rendering them correctly because the stub still followed the OLD
    rule. 10,013 green tests, five broken screens. A test fixture that decides
    the same thing production decides is the third expression of a rule CLAUDE.md
    allows one of — so it delegates now, and can only drift if production does.

    Unseeded keys still render as ``humanised_missing_key``, matching production,
    so a test cannot accidentally depend on a key that does not exist.
    """
    from shared.i18n_rendering import render_translation

    table = translations or {}

    def _get(key, language=None, *args, **kwargs):
        normalized = i18n_module.i18n.normalize_language(language)
        value = table.get((normalized, key)) or table.get(key)
        if value is None:
            return i18n_module.i18n.humanised_missing_key(key)
        return render_translation(key, value, args, kwargs)

    monkeypatch.setattr(i18n_module.i18n, "get", _get)
    _patch_bot_modules(monkeypatch, "i18n", i18n_module.i18n)


# ---------------------------------------------------------------------------
# Building real Updates
# ---------------------------------------------------------------------------
