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
    "staff.bottles.notify.transfer_received_lines", "staff.bottles.notify.join_requested",
    "staff.bottles.notify.join_declined", "staff.bottles.join_request_approve",
    "staff.bottles.join_request_decline", "staff.bottles.joined_session",
    "staff.bottles.joined_session_info", "staff.delivery.transfer_confirm_lines_button",
    "staff.delivery.transfer_custom_count_button", "staff.delivery.transfer_reject_button",
    "staff.common.unknown_driver", "staff.back",
    "staff.bottles.notify.transfer_confirmed", "staff.bottles.notify.trip_auto_closed",
    "staff.bottles.notify.transfer_disputed", "staff.bottles.notify.transfer_disputed_no_admin",
    "staff.bottles.notify.transfer_confirmed_lines", "staff.bottles.notify.transfer_disputed_lines",
    "staff.bottles.notify.transfer_disputed_lines_no_admin",
    "staff.bottles.notify.transfer_rejected", "staff.bottles.notify.transfer_rejected_trip_closed",
    "staff.delivery.transfer_line_full", "staff.delivery.transfer_line_empty", "staff.delivery.transfer_line_product",
)

# Every line of a transfer as the backend publishes it (`transfer_lines`): 5 full, 2 empty and 3 x 10 L, with what
# the receiver counted (4 empties) once he has.
COUNTED_LINES = [
    {"kind": "full", "product_id": None, "product_name": None, "declared": 5, "confirmed": 5},
    {"kind": "empty", "product_id": None, "product_name": None, "declared": 2, "confirmed": 1},
    {"kind": "product", "product_id": 9, "product_name": "10 Litr suv", "declared": 3, "confirmed": 3},
]

PAYLOADS = {
    # A push queued before the deploy that published `lines`: the two numbers alone.
    "transfer_received": {"id": 31, "declared_quantity": 4, "declared_empty_quantity": 2, "sender_name": "Ali <&> Co"},
    "transfer_confirmed": {
        "id": 31, "receiver_name": "Ali <&> Co", "confirmed_full": 5, "confirmed_empty": 2, "auto_closed": True,
    },
    "transfer_disputed": {
        "id": 31, "receiver_name": "Ali <&> Co", "declared_full": 5, "declared_empty": 2,
        "confirmed_full": 4, "confirmed_empty": 1, "admins_alerted": True,
    },
    # bottle_notifications.notify_transfer_rejected_to_sender's payload.
    "transfer_rejected": {
        "id": 31, "transfer_ref": "a1b2c3d4-0000-4000-8000-000000000031", "receiver_name": "Ali <&> Co",
        "lines": [
            {"kind": "full", "product_id": None, "product_name": None, "declared": 5, "confirmed": None},
            {"kind": "empty", "product_id": None, "product_name": None, "declared": 2, "confirmed": None},
            {"kind": "product", "product_id": 9, "product_name": "10 Litr suv", "declared": 3, "confirmed": None},
        ],
        "sender_trip_was_closed": True,
    },
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


def test_a_push_without_lines_renders_the_two_numbers_and_three_buttons(server, real_copy):
    """A push queued before the deploy carries no `lines`: it reads as its full bottles and empties, under the
    inbox's three buttons."""
    _post(server, "transfer_received")
    sent = _sent(server)
    assert sent["text"] == "📥 Ali &lt;&amp;&gt; Co sent you: 18.9 L full 4 · empty 2"
    buttons = [(button.text, button.callback_data) for row in sent["reply_markup"].inline_keyboard for button in row]
    assert buttons[:3] == [
        ("✅ Confirm 18.9 L full 4 · empty 2", "staff_transfer_confirm_31"),
        ("✏️ Enter other numbers", "staff_transfer_custom_31"),
        ("❌ Reject", "staff_transfer_reject_31"),
    ]


def test_a_push_with_lines_names_every_line(server, real_copy):
    _post(server, "transfer_received", "uz", payload={
        **PAYLOADS["transfer_received"], "sender_name": "Aziz",
        "lines": [{**line, "confirmed": None} for line in COUNTED_LINES],
    })
    assert _sent(server)["text"] == "📥 Aziz sizga yubordi: 18,9 L to'la 5 · bo'sh 2 · 10 Litr suv 3"


@pytest.mark.parametrize(
    "sender_trip_was_closed, closed_line",
    [(False, ""), (True, "\nReys yopilgan edi, farq qayta hisoblandi.")],
    ids=["trip-open", "trip-closed"],
)
def test_the_rejected_push_names_the_lines_and_the_closed_trip(server, real_copy, sender_trip_was_closed, closed_line):
    """`notify_transfer_rejected_to_sender`'s payload, word for word as the spec's §6.14 rows: who rejected it,
    the transfer's first eight characters, every line as declared, and the closed-trip line only when the
    backend says the trip had closed."""
    payload = {
        "id": 31, "transfer_ref": "a1b2c3d4-0000-4000-8000-000000000031", "receiver_name": "Bobur",
        "lines": [
            {"kind": "full", "product_id": None, "product_name": None, "declared": 10, "confirmed": None},
            {"kind": "empty", "product_id": None, "product_name": None, "declared": 4, "confirmed": None},
            {"kind": "product", "product_id": 9, "product_name": "10 Litr suv", "declared": 6, "confirmed": None},
        ],
        "sender_trip_was_closed": sender_trip_was_closed,
    }

    assert _post(server, "transfer_rejected", "uz", payload=payload).status == 200

    sent = _sent(server)
    assert sent["text"] == (
        "❌ Bobur #a1b2c3d4 o'tkazmangizni rad etdi. 18,9 L to'la 10 · bo'sh 4 · 10 Litr suv 6 reysingizga qaytdi."
        + closed_line
    )
    assert sent["reply_markup"] is None


def test_a_join_request_push_carries_approve_and_decline_for_that_session_and_driver(server, real_copy):
    _post(server, "join_requested")
    assert _callbacks(_sent(server)["reply_markup"]) == ["bottles_jr_ok_12_77", "bottles_jr_no_12_77"]


@pytest.mark.parametrize(
    "event", ("join_approved", "join_declined", "transfer_confirmed", "transfer_disputed", "transfer_rejected")
)
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


def _render_with(server, event, language, **flags):
    server.bot_app.bot.send_message.reset_mock()
    _post(server, event, language, payload={**PAYLOADS[event], **flags})
    return _sent(server)["text"]


def _last_sentence(key, language):
    return i18n.translations[language][key].rsplit(". ", 1)[-1]


# A push queued before the deploy names the two numbers; one from after it carries every line.
WITH_AND_WITHOUT_LINES = pytest.mark.parametrize(
    "lines, suffix", [({}, ""), ({"lines": COUNTED_LINES}, "_lines")], ids=["two-numbers", "every-line"]
)


@WITH_AND_WITHOUT_LINES
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_trip_line_follows_the_backends_auto_closed_flag(server, real_copy, language, lines, suffix):
    trip_line = i18n.translations[language]["staff.bottles.notify.trip_auto_closed"]

    assert _render_with(server, "transfer_confirmed", language, auto_closed=True, **lines).endswith("\n" + trip_line)
    assert trip_line not in _render_with(server, "transfer_confirmed", language, auto_closed=False, **lines)


@WITH_AND_WITHOUT_LINES
@pytest.mark.parametrize("language", LANGUAGES)
def test_an_admin_will_check_only_when_the_backend_alerted_one(server, real_copy, language, lines, suffix):
    will_check = _last_sentence(f"staff.bottles.notify.transfer_disputed{suffix}", language)
    tell_one = _last_sentence(f"staff.bottles.notify.transfer_disputed{suffix}_no_admin", language)

    alerted = _render_with(server, "transfer_disputed", language, admins_alerted=True, **lines)
    nobody = _render_with(server, "transfer_disputed", language, admins_alerted=False, **lines)

    assert alerted.endswith(will_check) and tell_one not in alerted
    assert nobody.endswith(tell_one) and will_check not in nobody


def test_a_count_push_with_lines_names_every_line_counted_and_sent(server, real_copy):
    """The sender's answer from a backend that publishes `lines`: the 10 L line is in it, as counted (and, on a
    dispute, as sent)."""
    confirmed = _render_with(server, "transfer_confirmed", "en", auto_closed=False, lines=COUNTED_LINES)
    disputed = _render_with(server, "transfer_disputed", "en", admins_alerted=True, lines=COUNTED_LINES)

    assert confirmed == "Ali &lt;&amp;&gt; Co confirmed 18.9 L full 5 · empty 1 · 10 Litr suv 3."
    assert disputed == (
        "Ali &lt;&amp;&gt; Co counted 18.9 L full 5 · empty 1 · 10 Litr suv 3; "
        "you sent 18.9 L full 5 · empty 2 · 10 Litr suv 3. An admin will check it."
    )
