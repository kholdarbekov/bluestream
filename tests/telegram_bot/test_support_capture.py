from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
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
    monkeypatch.setattr(bot_module, "api_client", fake_client)
    monkeypatch.setattr(bot_module, "get_auth_token", AsyncMock(return_value="tok-123"))

    update = DummyUpdate()
    update.message.text = "Hello, I need water"
    context = make_context()

    await bot_module.WaterBusinessBot._capture_support_message(
        SimpleNamespace(), update, context, "Hello, I need water"
    )

    fake_client.record_support_message.assert_awaited_once_with("tok-123", "Hello, I need water")
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
