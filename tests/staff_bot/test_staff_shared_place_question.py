"""The shared-place question on the driver's Delivered screen, through the real dispatcher.

A first-order customer who hands back empties may open a shared-place case; the driver is
asked one question under the "Delivered" message. These tests walk a real delivery to the
door, type the returned count, and read what the driver SEES and what the BACKEND receives.
Only the three harness seams are faked (Telegram transport, `_make_request`, bot SQL).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from staff_bot.api_client import api_client
from staff_bot.handlers.delivery import shared_place
from staff_bot.utils.formatters import format_currency

from tests.integration.reorder_reminder_builders import captured_logs
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (
    FakeDeliveryDesk,  # noqa: F401  (build_driver attaches it)
    _isolated_route_card_state,  # noqa: F401  (autouse fixture: fresh route-card locks per test)
    active_row,
    backend_calls,
    build_driver,
    capture_errors,
    copy_for,
    sign_in,
    texts,
    visible_answer_texts,
    walk_to_the_door,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_shared_place_translations.py"
QUESTION_URL = "/api/v1/staff/delivery/501/shared-place-question"


def _answer_url(case_id: int) -> str:
    return f"/api/v1/staff/shared-place-cases/{case_id}/answer"


def _seed_keys() -> dict:
    spec = importlib.util.spec_from_file_location("seed_staff_shared_place_translations", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {key: langs["en"] for key, langs in module.KEYS.items()}


BOTTLE_COPY = (
    "staff.error.api.shared_place_answer_refused",
    "staff.delivery.bottles_return_prompt",
    "staff.delivery.bottles_enter_count",
    "staff.delivery.bottles_all_returned",
    "staff.delivery.bottles_none_returned",
    "staff.delivery.bottles_zero_returned",
    "staff.delivery.view_bottle_balance",
    "staff.delivery.enter_bottle_count",
    "staff.delivery.invalid_bottle_count",
)

QUESTION = {
    "ask": True,
    "case_id": 9,
    "customer_first_name": "Jasur",
    "candidates": [
        {"index": 0, "first_name": "Akmal", "empties": 8, "distance_m": 14},
        {"index": 1, "first_name": "Dilshod", "empties": 2, "distance_m": 30},
        {"index": 2, "first_name": "Sardor", "empties": 1, "distance_m": 48},
    ],
}


async def _bot(monkeypatch, question=QUESTION):
    harness = await build_driver(monkeypatch, extra_keys=BOTTLE_COPY, translations=_seed_keys())
    harness.desk.active = [
        active_row(501, "BS-1001", expected_returnable_bottles=3, customer_bottle_balance=3)
    ]
    harness.desk.serve(501)
    if question is not None:
        harness.backend.route("GET", QUESTION_URL, lambda _call: question)
    return harness


async def _deliver_with_returns(harness, typed="3"):
    """Walk to the door, take the full cash, type the returned count. Returns the driver."""
    driver, labels = await sign_in(harness)
    await harness.send(driver.text(
        next(label for label in labels if label.strip().endswith(copy_for("staff.menu.active_deliveries")))
    ))
    await harness.send(driver.tap("staff_view_active_501"))
    await walk_to_the_door(harness, driver)
    await harness.send(driver.tap("staff_status_501_delivered"))
    await harness.send(driver.tap("staff_cash_full_501"))
    await harness.send(driver.tap("staff_bottles_custom_501"))
    harness.telegram.reset()
    await harness.send(driver.text(typed))
    return driver


def _pending(harness):
    return (harness.application.user_data.get(DEFAULT_DRIVER_TELEGRAM_ID) or {}).get(
        "pending_shared_place_note"
    )


def _plain_tail() -> str:
    return (
        f"✅ {copy_for('staff.delivery.delivered_success')}\n"
        f"💵 {copy_for('staff.delivery.cash_recorded').format(amount=format_currency(150000, language='en'))}"
    )


async def test_typed_return_shows_question_with_buttons(monkeypatch):
    harness = await _bot(monkeypatch)
    errors = capture_errors(harness)
    await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert not errors
    assert "Is Jasur the same as one of these?" in shown.text
    assert _plain_tail() in shown.text
    assert shown.callback_data() == [
        "staff_sp_9_p0", "staff_sp_9_p1", "staff_sp_9_p2", "staff_sp_9_none", "staff_sp_9_skip",
    ]
    assert shown.button_labels()[0] == "Akmal · 8 empties · 14 m"
    assert len(backend_calls(harness, "GET", QUESTION_URL)) == 1
    assert _pending(harness) is None


async def test_zero_returned_makes_no_question_call(monkeypatch):
    harness = await _bot(monkeypatch)
    await _deliver_with_returns(harness, typed="0")

    assert backend_calls(harness, "GET", QUESTION_URL) == []
    assert harness.telegram.last_shown().text.endswith(_plain_tail())
    assert harness.telegram.last_shown().callback_data() == ["staff_active_deliveries"]


async def test_failed_question_get_leaves_plain_delivered_message(monkeypatch):
    harness = await _bot(monkeypatch, question=None)
    harness.backend.route("GET", QUESTION_URL, lambda _call: staff_backend_failure("boom", 500))
    errors = capture_errors(harness)
    await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert not errors
    assert len(backend_calls(harness, "GET", QUESTION_URL)) == 1, "the question GET was never attempted"
    assert shown.text.endswith(_plain_tail())
    assert "\n\n" not in shown.text.split(_plain_tail())[1]
    assert shown.callback_data() == ["staff_active_deliveries"]


@pytest.mark.parametrize("question", [
    pytest.param({key: value for key, value in QUESTION.items() if key != "case_id"}, id="no-case-id"),
    pytest.param({**QUESTION, "case_id": "nine"}, id="case-id-not-a-number"),
    pytest.param({**QUESTION, "case_id": None}, id="case-id-null"),
    pytest.param({**QUESTION, "case_id": "9"}, id="case-id-a-string"),
    pytest.param(
        {**QUESTION, "candidates": [{"first_name": "Akmal", "empties": 8, "distance_m": 14}]},
        id="candidate-without-index",
    ),
    pytest.param(
        {**QUESTION, "candidates": [{"index": "0", "first_name": "Akmal", "empties": 8, "distance_m": 14}]},
        id="candidate-index-a-string",
    ),
    pytest.param(
        # A button the tap regex (p0-p2) could never route back.
        {**QUESTION, "candidates": [{"index": 3, "first_name": "Akmal", "empties": 8, "distance_m": 14}]},
        id="candidate-index-out-of-range",
    ),
])
async def test_malformed_question_leaves_plain_delivered_message(monkeypatch, caplog, question):
    """The delivery is already recorded when the question is drawn: a payload the bot cannot
    draw must leave today's Delivered screen, never a generic error after a delivery that landed."""
    harness = await _bot(monkeypatch, question=question)
    errors = capture_errors(harness)

    with captured_logs(caplog, shared_place.logger, logging.WARNING):
        await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert len(backend_calls(harness, "GET", QUESTION_URL)) == 1
    assert shown.text.endswith(_plain_tail())
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None
    assert not errors
    assert copy_for("staff.error_occurred") not in texts(harness) + visible_answer_texts(harness)
    assert [
        record.levelname for record in caplog.records
        if record.name == shared_place.logger.name and "malformed" in record.getMessage()
    ] == ["WARNING"]


async def test_unrouted_question_is_treated_as_no_question(monkeypatch):
    harness = await _bot(monkeypatch, question=None)
    await _deliver_with_returns(harness)

    assert len(backend_calls(harness, "GET", QUESTION_URL)) == 1
    shown = harness.telegram.last_shown()
    assert shown.text.endswith(_plain_tail())
    assert shown.callback_data() == ["staff_active_deliveries"]


async def test_ask_false_leaves_plain_delivered_message(monkeypatch):
    harness = await _bot(monkeypatch, question={"ask": False})
    await _deliver_with_returns(harness)

    assert harness.telegram.last_shown().text.endswith(_plain_tail())
    assert harness.telegram.last_shown().callback_data() == ["staff_active_deliveries"]


async def test_tap_pick_puts_answer_and_shows_note_prompt(monkeypatch):
    harness = await _bot(monkeypatch)
    harness.backend.route("PUT", _answer_url(9), lambda _call: {
        "case_id": 9, "answer": "picked", "picked_first_name": "Dilshod", "note_saved": False,
    })
    errors = capture_errors(harness)
    driver = await _deliver_with_returns(harness)
    harness.telegram.reset()

    await harness.send(driver.tap("staff_sp_9_p1"))

    puts = backend_calls(harness, "PUT", _answer_url(9))
    assert [call.data for call in puts] == [{"answer": "picked", "candidate_index": 1}]
    shown = harness.telegram.last_shown()
    assert shown.text == "✅ Dilshod\nAnything for the admin? Type it, or Skip."
    assert shown.callback_data() == ["staff_sp_9_done"]
    pending = _pending(harness)
    assert {k: pending[k] for k in ("case_id", "mode")} == {"case_id": 9, "mode": "note"}
    assert pending["chat_id"] and pending["message_id"]
    assert not errors


async def test_tap_none_and_tap_skip(monkeypatch):
    harness = await _bot(monkeypatch)
    harness.backend.route("PUT", _answer_url(9), lambda call: {
        "case_id": 9, "answer": call.data["answer"], "picked_first_name": None, "note_saved": False,
    })
    driver = await _deliver_with_returns(harness)

    harness.telegram.reset()
    await harness.send(driver.tap("staff_sp_9_none"))
    assert backend_calls(harness, "PUT", _answer_url(9))[-1].data == {"answer": "none"}
    assert harness.telegram.last_shown().text == "✅ None of these\nAnything for the admin? Type it, or Skip."
    assert harness.telegram.last_shown().callback_data() == ["staff_sp_9_done"]
    assert _pending(harness) is not None

    harness.telegram.reset()
    await harness.send(driver.tap("staff_sp_9_skip"))
    assert backend_calls(harness, "PUT", _answer_url(9))[-1].data == {"answer": "skipped"}
    shown = harness.telegram.last_shown()
    assert shown.text == "Skipped."
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None


async def test_done_closes_the_note_prompt_without_a_put(monkeypatch):
    harness = await _bot(monkeypatch)
    harness.backend.route("PUT", _answer_url(9), lambda _call: {
        "case_id": 9, "answer": "picked", "picked_first_name": "Akmal", "note_saved": False,
    })
    driver = await _deliver_with_returns(harness)
    await harness.send(driver.tap("staff_sp_9_p0"))
    puts_before = len(backend_calls(harness, "PUT", _answer_url(9)))

    harness.telegram.reset()
    await harness.send(driver.tap("staff_sp_9_done"))

    assert len(backend_calls(harness, "PUT", _answer_url(9))) == puts_before
    shown = harness.telegram.last_shown()
    assert shown.text == "Thanks — sent to the admin."
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None


async def test_refused_answer_shows_settled_copy(monkeypatch):
    harness = await _bot(monkeypatch)
    harness.backend.route("PUT", _answer_url(9), lambda _call: staff_backend_failure(
        "This report is already settled", 409, error_code="SHARED_PLACE_ANSWER_REFUSED",
    ))
    errors = capture_errors(harness)
    driver = await _deliver_with_returns(harness)
    harness.telegram.reset()

    await harness.send(driver.tap("staff_sp_9_p0"))

    shown = harness.telegram.last_shown()
    assert shown.text == copy_for("staff.error.api.shared_place_answer_refused")
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None
    assert not errors
    assert copy_for("staff.error_occurred") not in texts(harness) + visible_answer_texts(harness)


async def test_name_is_escaped_and_callbacks_fit_64_bytes(monkeypatch):
    question = {
        "ask": True,
        "case_id": 2_147_483_647,
        "customer_first_name": "<b>Ali & Vali</b>",
        "candidates": [
            {"index": 0, "first_name": "<Ali & Vali>", "empties": 8, "distance_m": 14},
            {"index": 1, "first_name": "   ", "empties": 2, "distance_m": 30},
        ],
    }
    harness = await _bot(monkeypatch, question=question)
    harness.backend.route("PUT", _answer_url(2_147_483_647), lambda _call: {
        "case_id": 2_147_483_647, "answer": "picked", "picked_first_name": "<Ali & Vali>",
        "note_saved": False,
    })
    driver = await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert "Is &lt;b&gt;Ali &amp; Vali&lt;/b&gt; the same as one of these?" in shown.text
    assert shown.button_labels()[0] == "<Ali & Vali> · 8 empties · 14 m"
    assert shown.button_labels()[1] == "— · 2 empties · 30 m"
    assert max(len(c.encode()) for c in shown.callback_data()) <= 64

    harness.telegram.reset()
    await harness.send(driver.tap("staff_sp_2147483647_p0"))
    assert harness.telegram.last_shown().text.startswith("✅ &lt;Ali &amp; Vali&gt;\n")
    assert max(len(c.encode()) for c in harness.telegram.last_shown().callback_data()) <= 64


async def test_blank_customer_name_shows_a_dash(monkeypatch):
    harness = await _bot(monkeypatch, question={**QUESTION, "customer_first_name": ""})
    await _deliver_with_returns(harness)

    assert "Is — the same as one of these?" in harness.telegram.last_shown().text


async def test_double_tap_same_candidate_is_quiet(monkeypatch):
    harness = await _bot(monkeypatch)
    harness.backend.route("PUT", _answer_url(9), lambda _call: {
        "case_id": 9, "answer": "picked", "picked_first_name": "Akmal", "note_saved": False,
    })
    errors = capture_errors(harness)
    driver = await _deliver_with_returns(harness)
    await harness.send(driver.tap("staff_sp_9_p0"))

    harness.telegram.fail("editMessageText", "Message is not modified", 400)
    await harness.send(driver.tap("staff_sp_9_p0"))

    prompts = [c for c in harness.telegram.delivered if "Anything for the admin?" in c.text]
    assert len(prompts) == 1
    assert not errors
    assert copy_for("staff.error_occurred") not in texts(harness) + visible_answer_texts(harness)
    assert _pending(harness) is not None


async def test_slow_question_get_shows_plain_delivered_message(monkeypatch):
    harness = await _bot(monkeypatch)
    monkeypatch.setattr(shared_place, "QUESTION_FETCH_TIMEOUT_S", 0.05)
    real = api_client._make_request

    cancelled = []

    async def slow(method, endpoint, *args, **kwargs):
        if endpoint == QUESTION_URL:
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                cancelled.append(endpoint)
                raise
        return await real(method, endpoint, *args, **kwargs)

    monkeypatch.setattr(api_client, "_make_request", slow)
    errors = capture_errors(harness)
    await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert not errors
    assert cancelled == [QUESTION_URL], "the question GET was never attempted, or never timed out"
    assert shown.text.endswith(_plain_tail())
    assert shown.callback_data() == ["staff_active_deliveries"]


async def test_no_candidates_screen_arms_note_mode(monkeypatch):
    harness = await _bot(monkeypatch, question={**QUESTION, "candidates": []})
    await _deliver_with_returns(harness)

    shown = harness.telegram.last_shown()
    assert "Jasur returned empties. Anything for the admin? Type it, or Skip." in shown.text
    assert shown.callback_data() == ["staff_sp_9_skip"]
    pending = _pending(harness)
    assert pending["case_id"] == 9 and pending["mode"] == "answer_none"
    assert pending["chat_id"] and pending["message_id"]


async def test_full_return_button_path_asks_the_question_too(monkeypatch):
    """The callback path (no typing) shares the same success screen."""
    harness = await _bot(monkeypatch)
    driver, labels = await sign_in(harness)
    await harness.send(driver.text(
        next(label for label in labels if label.strip().endswith(copy_for("staff.menu.active_deliveries")))
    ))
    await harness.send(driver.tap("staff_view_active_501"))
    await walk_to_the_door(harness, driver)
    await harness.send(driver.tap("staff_status_501_delivered"))
    await harness.send(driver.tap("staff_cash_full_501"))
    harness.telegram.reset()
    await harness.send(driver.tap("staff_bottles_full_501"))

    shown = harness.telegram.last_shown()
    assert "Is Jasur the same as one of these?" in shown.text
    assert shown.callback_data()[0] == "staff_sp_9_p0"
