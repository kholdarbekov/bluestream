"""POST /internal/bottle-event renders each bottle push in the driver's language.

The copy comes from scripts/seed_staff_translations.py through `_curated_value`,
so an edit to the seed cannot leave this file asserting copy that no longer ships.
Sync tests drive the coroutine with asyncio.run (no pytest-asyncio in this repo).
"""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from shared.staff_constants import BOTTLE_EVENTS
from staff_bot.i18n import i18n

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations_bottle_webhook",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py",
)
_SEED = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_SEED)

LANGUAGES = ("en", "uz", "ru")
KEYS = (
    "staff.bottles.notify.transfer_received", "staff.bottles.notify.join_requested",
    "staff.bottles.notify.join_declined", "staff.bottles.join_request_approve",
    "staff.bottles.join_request_decline", "staff.bottles.joined_session",
    "staff.bottles.joined_session_info", "staff.delivery.transfer_confirm_button",
    "staff.delivery.transfer_custom_count_button", "staff.common.unknown_driver", "staff.back",
)

PAYLOADS = {
    "transfer_received": {"id": 31, "declared_quantity": 4, "sender_name": "Ali <&> Co"},
    "join_requested": {"session_id": 12, "requester_id": 77, "requester_name": "Ali <&> Co"},
    "join_approved": {"session_id": 12, "owner_name": "Ali <&> Co"},
    "join_declined": {"session_id": 12, "owner_name": "Ali <&> Co"},
}


@pytest.fixture
def real_copy(monkeypatch):
    table = {lang: {key: _SEED._curated_value(key, lang) for key in KEYS} for lang in LANGUAGES}
    for lang, rows in table.items():
        for key, value in rows.items():
            assert value, f"{key} is not seeded in {lang}"
    monkeypatch.setattr(i18n, "translations", table)


@pytest.fixture
def server():
    from staff_bot.webhook_server import StaffWebhookServer

    instance = StaffWebhookServer()
    instance.bot_app = MagicMock()
    instance.bot_app.bot.send_message = AsyncMock()
    return instance


def _post(server, event, language="en", *, signed=True, payload=None):
    request = MagicMock()
    request.path = "/internal/bottle-event"
    request.json = AsyncMock(return_value={
        "event_id": f"bottle-event:{event}", "telegram_id": 777000111, "event": event,
        "payload": payload if payload is not None else dict(PAYLOADS.get(event, {})),
    })
    with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=signed)), \
         patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
         patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
         patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value=language)):
        return asyncio.run(server.bottle_event_handler(request))


def _sent(server):
    return server.bot_app.bot.send_message.await_args.kwargs


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_the_route_has_its_own_rate_limit_bucket(server):
    assert "/internal/bottle-event" in server._rate_limiters


def test_an_unsigned_push_is_refused(server, real_copy):
    assert _post(server, "transfer_received", signed=False).status == 401
    server.bot_app.bot.send_message.assert_not_awaited()


def test_an_unknown_event_is_refused(server, real_copy):
    assert _post(server, "something_else").status == 400


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("event", BOTTLE_EVENTS)
def test_every_event_renders_escaped_names_and_no_unfilled_placeholder(server, real_copy, event, language):
    assert _post(server, event, language).status == 200
    kwargs = _sent(server)
    assert kwargs["chat_id"] == 777000111
    assert kwargs["parse_mode"] == "HTML"
    assert "Ali &lt;&amp;&gt; Co" in kwargs["text"]
    assert "<&>" not in kwargs["text"] and "{" not in kwargs["text"]


def test_a_transfer_push_carries_the_inbox_buttons(server, real_copy):
    _post(server, "transfer_received")
    assert _callbacks(_sent(server)["reply_markup"])[:2] == ["staff_transfer_confirm_31_4", "staff_transfer_custom_31"]
    assert "4" in _sent(server)["text"]


def test_a_join_request_push_carries_approve_and_decline_for_that_session_and_driver(server, real_copy):
    _post(server, "join_requested")
    assert _callbacks(_sent(server)["reply_markup"]) == ["bottles_jr_ok_12_77", "bottles_jr_no_12_77"]


@pytest.mark.parametrize("event", ("join_approved", "join_declined"))
def test_an_answer_needs_no_buttons(server, real_copy, event):
    _post(server, event)
    assert _sent(server)["reply_markup"] is None


def test_a_failed_send_frees_the_slot_and_answers_502_so_the_task_retries(server, real_copy):
    server.bot_app.bot.send_message = AsyncMock(side_effect=RuntimeError("telegram hiccup"))
    with patch.object(server, "_release_event", AsyncMock()) as release:
        response = _post(server, "transfer_received")
    assert response.status == 502
    release.assert_awaited_once()


def test_a_driver_who_blocked_the_bot_keeps_the_slot(server, real_copy):
    server.bot_app.bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
    with patch.object(server, "_release_event", AsyncMock()) as release:
        response = _post(server, "transfer_received")
    assert response.status == 200
    release.assert_not_awaited()


def _post_dedup(server, event_id, event="join_requested"):
    """Like `_post` but the real `_is_duplicate_event` runs, with Redis unavailable."""
    server._redis_connected = False
    request = MagicMock()
    request.path = "/internal/bottle-event"
    request.json = AsyncMock(return_value={
        "event_id": event_id, "telegram_id": 777000111, "event": event,
        "payload": dict(PAYLOADS.get(event, {})),
    })
    with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
         patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
         patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")):
        return asyncio.run(server.bottle_event_handler(request))


def test_without_redis_distinct_events_with_the_same_payload_both_send(server, real_copy):
    # A genuine re-request after a decline carries the same payload but a new event id.
    _post_dedup(server, "evt-1")
    _post_dedup(server, "evt-2")
    assert server.bot_app.bot.send_message.await_count == 2


def test_without_redis_the_same_event_id_sends_once(server, real_copy):
    _post_dedup(server, "evt-1")
    _post_dedup(server, "evt-1")
    assert server.bot_app.bot.send_message.await_count == 1
