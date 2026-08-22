"""Drive the staff bot the way Telegram drives it.

The sibling of ``tests/telegram_bot/ptb_harness.py``, and it exists for the
same reason: every test under ``tests/staff_bot/`` today calls a handler
coroutine directly, which cannot see the wiring — whether a rendered button is
registered, whether it is registered in the state the driver is parked in,
which handler group wins, or what a real Telegram rejection does to the flow.

The staff bot leans harder on that wiring than the customer bot does. Its menu
is a REPLY keyboard matched by localized-label regexes compiled once at
handler-build time, so a translation that changes shape silently makes a menu
button dead — a class of bug this project has already shipped twice
(``staff_bot_english_leak_classes``, ``staff_bot_text_router_state_leak``).

Seams, one per kind of I/O:

1. :class:`~tests.telegram_bot.ptb_harness.FakeTelegramTransport` (shared).
2. ``staff_bot.api_client.api_client._make_request``.
3. ``staff_bot.database.db_manager`` methods, patched on the instance because
   handler objects capture it inside their own ``StaffUserRepository``.

``flow_state`` is configured with ``None``, exactly as production does when
Redis is unreachable, so its documented degraded path is what runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from telegram import Update
from telegram.ext import Application, ConversationHandler

from tests.bot_dispatcher_harness import (  # noqa: F401  (re-exported)
    _is_catch_all,
    FakeTelegramTransport,
    TelegramCall,
    UpdateFactory,
)


DEFAULT_DRIVER_TELEGRAM_ID = 800100200
DEFAULT_DRIVER_CHAT_ID = 800100200


@dataclass
class StaffBackendCall:
    method: str
    endpoint: str
    data: Optional[dict] = None
    params: Optional[dict] = None


class FakeStaffBackend:
    """Routes ``staff_bot.api_client._make_request`` against memory."""

    def __init__(self):
        self.calls: list[StaffBackendCall] = []
        self.routes: dict[tuple[str, str], Callable[[StaffBackendCall], Any]] = {}
        self.orders: dict[int, dict] = {}

    def route(self, method: str, endpoint: str, responder):
        self.routes[(method.upper(), endpoint)] = responder

    async def handle(self, method, endpoint, token=None, data=None, params=None, **_kwargs):
        call = StaffBackendCall(method.upper(), endpoint, data, params)
        self.calls.append(call)

        responder = self.routes.get((call.method, endpoint))
        body = responder(call) if responder is not None else self._default(call)

        if isinstance(body, _StaffFailure):
            return _staff_response(
                False, error=body.error, status_code=body.status_code, error_code=body.error_code
            )
        return _staff_response(True, data=body)

    def _default(self, call: StaffBackendCall) -> Any:
        if call.endpoint == "/api/v1/auth/staff-login":
            return {
                "data": {
                    "access_token": "staff-access-token",
                    "refresh_token": "staff-refresh-token",
                    "user": {"id": 55, "role": "delivery"},
                }
            }
        if call.endpoint.startswith("/api/v1/delivery/orders"):
            return {"data": {"orders": list(self.orders.values())}}
        return {"data": {}}


@dataclass
class _StaffFailure:
    error: str
    status_code: int = 500
    error_code: Optional[str] = None


def staff_backend_failure(error: str, status_code: int = 500, error_code: str = None):
    return _StaffFailure(error=error, status_code=status_code, error_code=error_code)


def _staff_response(success, data=None, error=None, status_code=200, error_code=None):
    from staff_bot.api_client import APIResponse

    return APIResponse(
        success=success,
        data=data,
        error=error,
        status_code=status_code,
        error_code=error_code,
    )


@dataclass
class FakeStaffDatabase:
    """The handful of queries the staff bot issues directly."""

    staff_user: dict = field(
        default_factory=lambda: {
            "id": 55,
            "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
            "first_name": "Aziz",
            "last_name": "Driver",
            "phone": "+998901112233",
            "preferred_language": "uz",
            "role": "delivery",
            "status": "active",
            "staff_roles": '["delivery"]',
            "staff_bot_state": "{}",
        }
    )
    executed: list[str] = field(default_factory=list)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def execute(self, query, *args):
        self.executed.append(query)
        if "staff_bot_state" in query and args:
            self.staff_user["staff_bot_state"] = args[0]
        return "UPDATE 1"

    async def fetchone(self, query, *args):
        if "FROM users" in query:
            return dict(self.staff_user)
        return None

    async def fetchall(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        if "preferred_language" in query:
            return self.staff_user.get("preferred_language")
        return None


class FakeStaffTokenManager:
    def __init__(self):
        self.redis = None
        self._connected = False

    async def get_valid_token(self, *_args, **_kwargs):
        return "staff-access-token"

    async def store_tokens(self, *_args, **_kwargs):
        return True

    async def invalidate_tokens(self, *_args, **_kwargs):
        return True


@dataclass
class StaffBotHarness:
    application: Application
    telegram: FakeTelegramTransport
    backend: FakeStaffBackend
    database: FakeStaffDatabase

    def updates(self, **kwargs) -> UpdateFactory:
        kwargs.setdefault("user_id", DEFAULT_DRIVER_TELEGRAM_ID)
        kwargs.setdefault("chat_id", DEFAULT_DRIVER_CHAT_ID)
        return UpdateFactory(bot=self.application.bot, **kwargs)

    async def send(self, update: Update):
        await self.application.process_update(update)
        return update

    def conversation(self, name: str) -> ConversationHandler:
        for group in self.application.handlers.values():
            for handler in group:
                if isinstance(handler, ConversationHandler) and handler.name == name:
                    return handler
        raise AssertionError(f"no ConversationHandler named {name!r} is registered")

    def conversation_names(self) -> list[str]:
        return [
            handler.name
            for group in self.application.handlers.values()
            for handler in group
            if isinstance(handler, ConversationHandler)
        ]

    def conversation_state(
        self,
        name: str,
        chat_id=DEFAULT_DRIVER_CHAT_ID,
        user_id=DEFAULT_DRIVER_TELEGRAM_ID,
    ):
        return self.conversation(name)._conversations.get((chat_id, user_id))

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


async def build_staff_harness(monkeypatch, *, translations=None, database=None) -> StaffBotHarness:
    from telegram.ext import ApplicationBuilder

    from staff_bot import api_client as staff_api_module
    from staff_bot import database as staff_db_module
    from staff_bot import i18n as staff_i18n_module
    from staff_bot.utils import flow_state, route_card_state

    telegram = FakeTelegramTransport()
    backend = FakeStaffBackend()
    db = database or FakeStaffDatabase()

    for method in ("connect", "disconnect", "execute", "fetchone", "fetchall", "fetchval"):
        monkeypatch.setattr(staff_db_module.db_manager, method, getattr(db, method))

    monkeypatch.setattr(staff_api_module.api_client, "_make_request", backend.handle)
    monkeypatch.setattr(staff_api_module.api_client, "start", _noop)
    monkeypatch.setattr(staff_api_module.api_client, "aclose", _noop)

    _install_staff_translations(monkeypatch, staff_i18n_module, translations)

    # Production's own degraded path when Redis is unreachable. Saved and
    # restored rather than stomped: leaving it set would silently downgrade
    # every later staff test in the same worker.
    previous_flow_redis = getattr(flow_state, "_redis", None)
    previous_card_redis = getattr(route_card_state, "_redis", None)
    monkeypatch.setattr(
        flow_state, "_redis", None, raising=False
    )
    monkeypatch.setattr(
        route_card_state, "_redis", None, raising=False
    )
    del previous_flow_redis, previous_card_redis

    # `route_card_state._locks` holds module-level asyncio.Lock objects keyed by
    # driver id. anyio gives each test a FRESH event loop, so a Lock created in
    # one test raises "bound to a different event loop" in the next — an
    # intermittent failure whose pairing depends on how --dist=loadfile happens
    # to shard the run. Every hand-written staff test that touches the route
    # card already clears this; doing it here means no future one has to know.
    route_card_state._locks.clear()

    from staff_bot.bot import StaffBot

    application = (
        ApplicationBuilder()
        .token("424242:STAFF-TEST-TOKEN")
        .request(telegram)
        .get_updates_request(telegram)
        .build()
    )

    staff_bot = StaffBot()
    staff_bot.application = application
    staff_bot.token_manager = FakeStaffTokenManager()
    application.bot_data["token_manager"] = staff_bot.token_manager

    await staff_bot._setup_handlers()
    await application.initialize()

    telegram.reset()
    return StaffBotHarness(application, telegram, backend, db)


async def _noop(*_args, **_kwargs):
    return None


def _install_staff_translations(monkeypatch, staff_i18n_module, translations):
    """Serve staff translations from memory.

    The staff bot's menu is a REPLY keyboard matched by regexes built from
    these very strings at handler-build time, so the table a test supplies is
    also the table the router matches against — the same coupling production
    has, and the reason a mis-seeded key kills a menu button there.

    Only the LOOKUP is faked; the RENDERING is production's own
    ``shared.i18n_rendering.render_translation`` — see the note on the customer
    bot's ``_install_translations`` for why a hand-rolled copy here is a bug
    waiting for production to move.
    """
    from shared.i18n_rendering import render_translation

    table = translations or {}
    real_get = staff_i18n_module.i18n.get

    def _get(key, language=None, *args, **kwargs):
        value = table.get((language, key)) or table.get(key)
        if value is None:
            return real_get(key, language, *args, **kwargs)
        return render_translation(key, value, args, kwargs)

    monkeypatch.setattr(staff_i18n_module.i18n, "get", _get)
