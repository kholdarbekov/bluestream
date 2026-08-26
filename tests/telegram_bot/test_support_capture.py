from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import support_capture as support_capture_module
from tests.telegram_bot.helpers import DummyUpdate, make_context


class _FakeClient:
    """async-context-manager fake exposing record_support_message."""
    def __init__(self):
        self.record_support_message = AsyncMock(return_value=SimpleNamespace(success=True))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_records_silently(monkeypatch):
    """Free text is recorded for the admin; no auto-acknowledgement is sent back."""
    fake_client = _FakeClient()
    # `_capture_support_message` now delegates entirely to
    # `support_capture.capture_support_message`, which resolves `api_client` and
    # `get_auth_token` from its OWN module namespace (bound at import time via
    # `from api_client import api_client` / `from utils import get_auth_token`)
    # — patching `bot_module`'s bindings of the same names does not reach it.
    monkeypatch.setattr(support_capture_module, "api_client", fake_client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-123"))

    update = DummyUpdate()
    update.message.text = "Hello, I need water"
    context = make_context()

    await bot_module.WaterBusinessBot._capture_support_message(
        SimpleNamespace(), update, context, "Hello, I need water"
    )

    fake_client.record_support_message.assert_awaited_once_with(
        "tok-123", content="Hello, I need water", message_type="text"
    )
    update.message.reply_text.assert_not_awaited()


class _ExplodingMessage:
    """A message whose `photo` access raises, standing in for whatever shape of
    Telegram message `build_support_payload`'s branching code has not yet seen.

    Regression guard for IMPORTANT 1 (task-4 review): `build_support_payload`
    used to be called OUTSIDE `capture_support_message`'s try/except, so a
    crash here escaped all the way past the caller and reached the customer as
    `telegram.error_occurred` — for a message they never asked us to send
    anywhere. That breaks the silent-capture invariant this module exists to
    uphold (see test_a_backend_500_on_capture_never_reaches_the_customer in
    tests/telegram_bot/test_support_and_text_routing.py).
    """
    text = "will never be read"
    caption = None
    document = None
    location = None
    venue = None
    voice = None
    video = None
    video_note = None
    audio = None
    forward_origin = None

    def __init__(self):
        self.reply_text = AsyncMock()

    @property
    def photo(self):
        raise RuntimeError("telegram sent a shape build_support_payload does not expect")


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_builder_crash_returns_false_instead_of_escaping_to_the_customer(monkeypatch):
    monkeypatch.setattr(support_capture_module, "api_client", _FakeClient())
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))

    update = DummyUpdate()
    update.message = _ExplodingMessage()
    context = make_context()

    result = await support_capture_module.capture_support_message(update, context)

    assert result is False
    update.message.reply_text.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_text_handler_routes_general_text_to_capture(monkeypatch):
    monkeypatch.setattr(bot_module.rate_limiter, "allow_request", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_module, "user_middleware", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(bot_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(bot_module.i18n, "get", lambda key, language, *a, **k: f"{key}:{language}")

    fake_self = SimpleNamespace(
        user_repository=SimpleNamespace(get_user_state=AsyncMock(return_value={})),
        _handle_otp_verification=AsyncMock(),
        _handle_contextual_input=AsyncMock(),
        _capture_support_message=AsyncMock(),
    )
    update = DummyUpdate()
    update.message.text = "spontaneous question"
    context = make_context()  # user_data = {}

    await bot_module.WaterBusinessBot._handle_text_message(fake_self, update, context)

    fake_self._capture_support_message.assert_awaited_once()
    assert fake_self._capture_support_message.await_args.args[2] == "spontaneous question"
    fake_self._handle_contextual_input.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_text_handler_skips_capture_when_awaiting_input(monkeypatch):
    monkeypatch.setattr(bot_module.rate_limiter, "allow_request", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_module, "user_middleware", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(bot_module.i18n, "get_user_language", AsyncMock(return_value="en"))

    fake_self = SimpleNamespace(
        user_repository=SimpleNamespace(get_user_state=AsyncMock(return_value={"awaiting_input": "edit_address_title"})),
        _handle_otp_verification=AsyncMock(),
        _handle_contextual_input=AsyncMock(),
        _capture_support_message=AsyncMock(),
    )
    update = DummyUpdate()
    update.message.text = "My Home"
    context = make_context()

    await bot_module.WaterBusinessBot._handle_text_message(fake_self, update, context)

    fake_self._handle_contextual_input.assert_awaited_once()
    fake_self._capture_support_message.assert_not_awaited()
