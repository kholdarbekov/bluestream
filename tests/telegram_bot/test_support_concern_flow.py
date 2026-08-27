from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import support_capture as support_capture_module
from handlers import support as support_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


class _FakeClient:
    """Async-context-manager fake exposing the api_client methods this flow uses."""

    def __init__(self, **methods):
        for name, val in methods.items():
            setattr(self, name, val)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_i18n(monkeypatch):
    monkeypatch.setattr(support_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(
        support_module.i18n, "get", lambda key, language=None, *a, **k: f"{key}:{language}"
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_start_arms_state_and_prompts(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = SimpleNamespace(arm_awaiting_input=AsyncMock())

    client = _FakeClient(
        get_order=AsyncMock(
            return_value=SimpleNamespace(
                success=True, data={"data": {"order": {"order_number": "ORD-1234"}}}
            )
        )
    )
    monkeypatch.setattr(support_module, "api_client", client)
    monkeypatch.setattr(support_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="report_issue_42")
    context = make_context()

    before = datetime.now(timezone.utc)
    await handler.start_order_issue_report(update, context)
    after = datetime.now(timezone.utc)

    update.callback_query.answer.assert_awaited_once()
    client.get_order.assert_awaited_once_with("tok-1", 42)

    # Was: update_user_state.await_args.args == (uid, {"awaiting_input": "support_message",
    # "support_order_id": 42, "support_order_number": "ORD-1234", "support_armed_at": ...}).
    # Same facts, now split across arm_awaiting_input's positional flow-name arg and
    # its companion kwargs.
    handler.user_repo.arm_awaiting_input.assert_awaited_once()
    call = handler.user_repo.arm_awaiting_input.await_args
    uid, flow = call.args
    assert uid == update.effective_user.id
    assert flow == "support_message"
    assert call.kwargs["support_order_id"] == 42
    assert call.kwargs["support_order_number"] == "ORD-1234"
    armed = datetime.fromisoformat(call.kwargs["support_armed_at"])
    assert before <= armed <= after

    # Prompt is a NEW message (delivered summary + its button stay tappable),
    # carrying a Cancel button.
    update.callback_query.message.reply_text.assert_awaited_once()
    markup = update.callback_query.message.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "support_cancel"


@pytest.mark.unit
@pytest.mark.anyio
async def test_start_falls_back_to_raw_id_when_lookup_fails(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = SimpleNamespace(arm_awaiting_input=AsyncMock())

    client = _FakeClient(
        get_order=AsyncMock(
            return_value=SimpleNamespace(success=False, data=None, error="boom")
        )
    )
    monkeypatch.setattr(support_module, "api_client", client)
    monkeypatch.setattr(support_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="report_issue_77")
    context = make_context()

    await handler.start_order_issue_report(update, context)

    # Was: update_user_state.await_args.args[1]["support_order_id"/"support_order_number"].
    # Same facts, now in arm_awaiting_input's companion kwargs.
    call = handler.user_repo.arm_awaiting_input.await_args
    assert call.kwargs["support_order_id"] == 77
    assert call.kwargs["support_order_number"] == "77"
    update.callback_query.message.reply_text.assert_awaited_once()


def _armed_repo(order_number="ORD-1234", armed_at=None):
    if armed_at is None:
        armed_at = datetime.now(timezone.utc).isoformat()
    state = {
        "awaiting_input": "support_message",
        "support_order_id": 42,
        "support_order_number": order_number,
        "support_armed_at": armed_at,
    }
    return SimpleNamespace(
        get_user_state=AsyncMock(return_value=state),
        disarm=AsyncMock(),
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_posts_prefixed_content_and_acks(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = _armed_repo()

    client = _FakeClient(record_support_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    # `handle_support_message` now delegates to `support_capture.capture_support_message`,
    # which resolves `api_client`/`get_auth_token` from ITS OWN module namespace —
    # patching `support_module`'s bindings no longer reaches it.
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.message.text = "the bottle arrived cracked"
    context = make_context()

    await handler.handle_support_message(update, context, "the bottle arrived cracked")

    client.record_support_message.assert_awaited_once_with(
        "tok-1", content="[Order #ORD-1234] the bottle arrived cracked", message_type="text"
    )
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}).
    # Same facts (same user id, the flow this handler owns), against the new method.
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')
    update.message.reply_text.assert_awaited_once_with("telegram.support.ack:en")


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_truncates_to_fit_serializer_cap(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = _armed_repo()

    client = _FakeClient(record_support_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    long_text = "x" * 5000
    update = DummyUpdate()
    update.message.text = long_text
    context = make_context()

    await handler.handle_support_message(update, context, long_text)

    posted = client.record_support_message.await_args.kwargs["content"]
    assert len(posted) == 4096
    assert posted.startswith("[Order #ORD-1234] ")
    update.message.reply_text.assert_awaited_once_with("telegram.support.ack:en")


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_no_token_sends_failed_no_ack(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = _armed_repo()

    client = _FakeClient(record_support_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value=None))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.message.text = "broken"
    context = make_context()

    await handler.handle_support_message(update, context, "broken")

    client.record_support_message.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with("telegram.support.send_failed:en")
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}).
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_api_error_sends_failed_no_ack(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = _armed_repo()

    client = _FakeClient(
        record_support_message=AsyncMock(return_value=SimpleNamespace(success=False, error="422"))
    )
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.message.text = "broken"
    context = make_context()

    await handler.handle_support_message(update, context, "broken")

    client.record_support_message.assert_awaited_once_with(
        "tok-1", content="[Order #ORD-1234] broken", message_type="text"
    )
    update.message.reply_text.assert_awaited_once_with("telegram.support.send_failed:en")
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}).
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_stale_falls_back_to_silent_no_prefix(monkeypatch):
    handler = support_module.SupportHandlers()
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    handler.user_repo = _armed_repo(armed_at=stale_at)

    client = _FakeClient(record_support_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.message.text = "much later message"
    context = make_context()

    await handler.handle_support_message(update, context, "much later message")

    # Silent capture: raw text, NO order prefix, NO ack.
    client.record_support_message.assert_awaited_once_with(
        "tok-1", content="much later message", message_type="text"
    )
    update.message.reply_text.assert_not_awaited()
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}).
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')


@pytest.mark.unit
@pytest.mark.anyio
async def test_capture_missing_order_number_falls_back_to_silent(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = SimpleNamespace(
        get_user_state=AsyncMock(return_value={
            "awaiting_input": "support_message",
            "support_order_id": 42,
            "support_armed_at": datetime.now(timezone.utc).isoformat(),
        }),
        disarm=AsyncMock(),
    )

    client = _FakeClient(record_support_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    monkeypatch.setattr(support_capture_module, "api_client", client)
    monkeypatch.setattr(support_capture_module, "get_auth_token", AsyncMock(return_value="tok-1"))
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.message.text = "no order reference here"
    context = make_context()

    await handler.handle_support_message(update, context, "no order reference here")

    client.record_support_message.assert_awaited_once_with(
        "tok-1", content="no order reference here", message_type="text"
    )
    update.message.reply_text.assert_not_awaited()
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}).
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')


@pytest.mark.unit
@pytest.mark.anyio
async def test_cancel_clears_state_and_confirms(monkeypatch):
    handler = support_module.SupportHandlers()
    handler.user_repo = SimpleNamespace(disarm=AsyncMock())
    _patch_i18n(monkeypatch)

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="support_cancel")
    context = make_context()

    await handler.cancel_issue_report(update, context)

    update.callback_query.answer.assert_awaited_once()
    # Was: update_user_state.assert_awaited_once_with(update.effective_user.id, {}) — an
    # unconditional blanket wipe. Converted (2026-08-26 Task 5, ruling 1): a stale Cancel
    # tap now only cancels the support report it names, via disarm(uid, 'support_message'),
    # and leaves any other armed flow (or an open address_draft) untouched.
    handler.user_repo.disarm.assert_awaited_once_with(update.effective_user.id, 'support_message')
    update.callback_query.edit_message_text.assert_awaited_once()
    assert update.callback_query.edit_message_text.await_args.kwargs["text"] == "telegram.support.cancelled:en"


@pytest.mark.unit
@pytest.mark.anyio
async def test_contextual_input_routes_support_message_to_flow_handler(monkeypatch):
    import bot as bot_module

    flow = SimpleNamespace(handle_support_message=AsyncMock())
    monkeypatch.setattr(bot_module, "support_flow_handlers", flow)

    fake_self = SimpleNamespace(user_repository=SimpleNamespace(update_user_state=AsyncMock()))
    update = DummyUpdate()
    update.message.text = "  bottle leaked  "
    context = make_context()

    await bot_module.WaterBusinessBot._handle_contextual_input(
        fake_self, update, context, {"awaiting_input": "support_message"}, "en"
    )

    # Text is .strip()'d by _handle_contextual_input before dispatch.
    flow.handle_support_message.assert_awaited_once_with(update, context, "bottle leaked")


@pytest.mark.unit
def test_callback_pattern_contract():
    import re

    report = re.compile(r"^report_issue_\d+$")
    cancel = re.compile(r"^support_cancel$")
    assert report.match("report_issue_42")
    assert not report.match("report_issue_")
    assert not report.match("report_issue_abc")
    assert cancel.match("support_cancel")
    # Not swallowed by (nor swallowing) the broad prefixes already in bot.py.
    assert not re.match(r"^order_", "report_issue_42")
    assert not re.match(r"^cancel_order", "support_cancel")
