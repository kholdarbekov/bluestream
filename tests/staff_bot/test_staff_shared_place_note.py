"""The optional note after the shared-place question, through the real dispatcher.

After the driver answers the question under the Delivered message, the next text they type is
their note for the admin. These tests walk a real delivery to the door, answer the question,
type, and read what the driver SEES and what the BACKEND receives. Only the three harness seams
are faked (Telegram transport, `_make_request`, bot SQL).

The note never outranks another flow: a menu tap, a new status change and a COD collection
started from its button each take the next text, and a bot that lost its user_data ignores it.
"""
from __future__ import annotations

import pytest

from staff_bot.utils.formatters import format_currency

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_driver_flows_after_state_loss import restart
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (
    FakeDeliveryDesk,  # noqa: F401  (build_driver attaches it)
    _isolated_route_card_state,  # noqa: F401  (autouse fixture: fresh route-card locks per test)
    active_row,
    backend_calls,
    build_driver,
    capture_errors,
    copy_for,
    menu_label,
    sign_in,
    texts,
    visible_answer_texts,
)
from tests.staff_bot.test_staff_shared_place_question import (
    BOTTLE_COPY,
    QUESTION,
    QUESTION_URL,
    _answer_url,
    _pending,
    _seed_keys,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOTE = "Akmal's coworker, same office"
THANKS = "Thanks — sent to the admin."
COD_STATEMENT = "/api/v1/staff/customers/42/cod-statement"

NOTE_COPY = BOTTLE_COPY + (
    "staff.menu.title",
    "staff.flow_timed_out",
    "staff.delivery.cod_collection_amount_prompt",
    "staff.delivery.cod_collection_note_prompt",
)

# The message the question was drawn on, and a second one for a later stop's question.
FIRST_MESSAGE, SECOND_MESSAGE = 7001, 7002


def _note_url(case_id: int) -> str:
    return f"/api/v1/staff/shared-place-cases/{case_id}/note"


def _question_url(delivery_id: int) -> str:
    return f"/api/v1/staff/delivery/{delivery_id}/shared-place-question"


async def _bot(monkeypatch, question=QUESTION):
    """A driver with stops 501 and 502, both owing a question (cases 9 and 10)."""
    harness = await build_driver(monkeypatch, extra_keys=NOTE_COPY, translations=_seed_keys())
    harness.desk.active = [
        active_row(501, "BS-1001", expected_returnable_bottles=3, customer_bottle_balance=3),
        active_row(502, "BS-1002", expected_returnable_bottles=3, customer_bottle_balance=3),
    ]
    harness.desk.serve(501, 502)
    harness.backend.route("GET", QUESTION_URL, lambda _call: question)
    harness.backend.route("GET", _question_url(502), lambda _call: {**QUESTION, "case_id": 10})
    return harness


async def _deliver(harness, driver, delivery_id: int, typed="3"):
    """Open the stop, walk it to the door, take the full cash, type the returned count."""
    await harness.send(driver.tap(f"staff_view_active_{delivery_id}"))
    for status in ("picked_up", "in_transit", "arrived"):
        await harness.send(driver.tap(f"staff_status_{delivery_id}_{status}"))
        await harness.send(driver.tap(f"staff_execute_status_{delivery_id}_{status}"))
    await harness.send(driver.tap(f"staff_status_{delivery_id}_delivered"))
    await harness.send(driver.tap(f"staff_cash_full_{delivery_id}"))
    await harness.send(driver.tap(f"staff_bottles_custom_{delivery_id}"))
    await harness.send(driver.text(typed))


async def _signed_in(harness):
    driver, labels = await sign_in(harness)
    await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    return driver, labels


def _answers(harness, case_id: int, picked_first_name="Akmal"):
    """The answer PUT for `case_id` succeeds, echoing what was sent."""
    harness.backend.route("PUT", _answer_url(case_id), lambda call: {
        "case_id": case_id,
        "answer": call.data["answer"],
        "picked_first_name": picked_first_name if call.data["answer"] == "picked" else None,
        "note_saved": "note" in call.data,
    })


def _notes(harness, case_id: int):
    harness.backend.route("PUT", _note_url(case_id), lambda _call: {"case_id": case_id, "note_saved": True})


async def _picked(monkeypatch, question=QUESTION):
    """Stop 501 delivered with 3 returned, Akmal picked: the note prompt is armed for case 9."""
    harness = await _bot(monkeypatch, question=question)
    _answers(harness, 9)
    _notes(harness, 9)
    driver, labels = await _signed_in(harness)
    await _deliver(harness, driver, 501)
    await harness.send(driver.tap("staff_sp_9_p0", message_id=FIRST_MESSAGE))
    assert _pending(harness)["case_id"] == 9
    harness.telegram.reset()
    return harness, driver, labels


def _shared_place_calls(harness):
    """Every write that reached a case."""
    return [call for call in harness.backend.calls if "/shared-place-cases/" in call.endpoint]


def _keyboard_drops(harness):
    return [
        (call.params.get("chat_id"), call.params.get("message_id"), call.params.get("reply_markup"))
        for call in harness.telegram.of("editMessageReplyMarkup")
    ]


# ---------------------------------------------------------------------------
# The note is written
# ---------------------------------------------------------------------------


async def test_typed_note_after_pick_puts_note(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    errors = capture_errors(harness)

    await harness.send(driver.text(NOTE))

    assert [call.data for call in backend_calls(harness, "PUT", _note_url(9))] == [{"note": NOTE}]
    shown = harness.telegram.last_shown()
    assert shown.text == THANKS
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None
    # The prompt's Skip button goes: the step is over.
    assert _keyboard_drops(harness) == [(DEFAULT_DRIVER_TELEGRAM_ID, FIRST_MESSAGE, None)]
    assert not errors


async def test_typed_note_without_candidates_answers_none_with_note(monkeypatch):
    harness = await _bot(monkeypatch, question={**QUESTION, "candidates": []})
    _answers(harness, 9)
    errors = capture_errors(harness)
    driver, _labels = await _signed_in(harness)
    await _deliver(harness, driver, 501)
    pending = _pending(harness)
    assert pending["mode"] == "answer_none"
    harness.telegram.reset()

    await harness.send(driver.text("Lives with his aunt next door"))

    assert [call.data for call in backend_calls(harness, "PUT", _answer_url(9))] == [
        {"answer": "none", "note": "Lives with his aunt next door"}
    ]
    assert backend_calls(harness, "PUT", _note_url(9)) == []
    assert harness.telegram.last_shown().text == THANKS
    assert harness.telegram.last_shown().callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None
    # The Delivered message's Skip button would file `skipped` over the answer just written.
    assert _keyboard_drops(harness) == [(pending["chat_id"], pending["message_id"], None)]
    assert not errors


async def test_note_over_500_chars_is_refused_locally_then_accepted(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    calls_before = len(harness.backend.calls)

    await harness.send(driver.text("x" * 501))

    assert harness.backend.calls[calls_before:] == [], "refused locally: nothing reaches the backend"
    assert harness.telegram.last_shown().text == "Please keep it to 500 characters or fewer."
    assert _pending(harness)["case_id"] == 9, "the note prompt must keep waiting"

    longest = "y" * 500
    await harness.send(driver.text(longest))

    assert [call.data for call in backend_calls(harness, "PUT", _note_url(9))] == [{"note": longest}]
    assert harness.telegram.last_shown().text == THANKS
    assert _pending(harness) is None


async def test_refused_note_shows_settled_copy(monkeypatch):
    """An admin settled the case between the pick and the note."""
    harness, driver, _labels = await _picked(monkeypatch)
    harness.backend.route("PUT", _note_url(9), lambda _call: staff_backend_failure(
        "This report is already settled", 409, error_code="SHARED_PLACE_ANSWER_REFUSED",
    ))
    errors = capture_errors(harness)

    await harness.send(driver.text(NOTE))

    shown = harness.telegram.last_shown()
    assert shown.text == copy_for("staff.error.api.shared_place_answer_refused")
    assert shown.callback_data() == ["staff_active_deliveries"]
    assert _pending(harness) is None
    assert _keyboard_drops(harness) == [(DEFAULT_DRIVER_TELEGRAM_ID, FIRST_MESSAGE, None)]
    assert copy_for("staff.error_occurred") not in texts(harness)
    assert not errors


async def test_failed_note_put_reports_the_error_and_stops_waiting(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    harness.backend.route("PUT", _note_url(9), lambda _call: staff_backend_failure("boom", 500))
    errors = capture_errors(harness)

    await harness.send(driver.text(NOTE))

    assert harness.telegram.last_shown().text == f"❌ {copy_for('staff.error.api.service_unavailable')}"
    assert _pending(harness) is None
    assert not errors

    # The next text is the driver's again: it reaches the menu, not another note PUT.
    await harness.send(driver.text("hello"))
    assert len(backend_calls(harness, "PUT", _note_url(9))) == 1
    assert harness.telegram.last_shown().text == copy_for("staff.menu.title")


async def test_a_failed_keyboard_drop_still_thanks_the_driver(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    harness.telegram.fail("editMessageReplyMarkup", "Bad Request: message to edit not found")
    errors = capture_errors(harness)

    await harness.send(driver.text(NOTE))

    assert harness.telegram.last_shown().text == THANKS
    assert _pending(harness) is None
    assert not errors


# ---------------------------------------------------------------------------
# Every other flow outranks the note
# ---------------------------------------------------------------------------


async def test_menu_tap_clears_note_and_next_text_goes_to_menu(monkeypatch):
    harness, driver, labels = await _picked(monkeypatch)

    await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    assert _pending(harness) is None

    await harness.send(driver.text(NOTE))

    assert backend_calls(harness, "PUT", _note_url(9)) == []
    assert harness.telegram.last_shown().text == copy_for("staff.menu.title")
    # The pick stays recorded: nothing after it reached the case.
    assert [call.data for call in _shared_place_calls(harness)] == [{"answer": "picked", "candidate_index": 0}]


async def test_cod_amount_typed_after_pick_goes_to_cod_flow_not_note(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    harness.backend.route("GET", COD_STATEMENT, lambda _call: {
        "customer_id": 42, "first_name": "Kamola", "cluster_delivered_outstanding_amount": 50000,
    })
    errors = capture_errors(harness)

    await harness.send(driver.tap("staff_cod_collect_custom_42", message_id=9_002))
    await harness.send(driver.text("50000"))

    assert backend_calls(harness, "PUT", _note_url(9)) == []
    flow = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["pending_cod_collection_flow"]
    assert flow["amount"] == 50000.0
    assert copy_for("staff.delivery.cod_collection_note_prompt").format(
        amount=format_currency(50000, language="en")
    ) in harness.telegram.last_shown().text
    assert not errors


async def test_new_status_change_drops_pending_note(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)

    await harness.send(driver.tap("staff_view_active_502"))
    await harness.send(driver.tap("staff_status_502_picked_up"))

    assert _pending(harness) is None

    await harness.send(driver.tap("staff_execute_status_502_picked_up"))
    await harness.send(driver.text(NOTE))

    assert backend_calls(harness, "PUT", _note_url(9)) == []
    assert harness.telegram.last_shown().text == copy_for("staff.menu.title")


async def test_restart_after_pick_ignores_text_quietly(monkeypatch):
    harness, driver, _labels = await _picked(monkeypatch)
    calls_before = len(harness.backend.calls)
    restart(harness)

    await harness.send(driver.text(NOTE))

    assert harness.backend.calls[calls_before:] == [], "no PUT, no login: the text is ignored"
    assert harness.telegram.of(
        "sendMessage", "editMessageText", "editMessageReplyMarkup", "answerCallbackQuery"
    ) == []
    assert copy_for("staff.flow_timed_out") not in texts(harness) + visible_answer_texts(harness)
    # The pick stays recorded.
    assert [call.data for call in _shared_place_calls(harness)] == [{"answer": "picked", "candidate_index": 0}]


# ---------------------------------------------------------------------------
# A button or a note for an OLDER case never disarms the newer case's prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stale_tap, case_9_answer", [
    # The old prompt's Skip, after case 9 was picked.
    ("staff_sp_9_done", "picked"),
    # An unanswered old question's Skip, filed now.
    ("staff_sp_9_skip", None),
    # An unanswered old question an admin has since settled.
    ("staff_sp_9_p0", "refused"),
])
async def test_old_message_button_keeps_newer_case_note_prompt(monkeypatch, stale_tap, case_9_answer):
    harness = await _bot(monkeypatch)
    if case_9_answer == "refused":
        harness.backend.route("PUT", _answer_url(9), lambda _call: staff_backend_failure(
            "This report is already settled", 409, error_code="SHARED_PLACE_ANSWER_REFUSED",
        ))
    else:
        _answers(harness, 9)
    _answers(harness, 10, picked_first_name="Dilshod")
    _notes(harness, 10)
    errors = capture_errors(harness)
    driver, _labels = await _signed_in(harness)
    await _deliver(harness, driver, 501)
    if case_9_answer == "picked":
        await harness.send(driver.tap("staff_sp_9_p0", message_id=FIRST_MESSAGE))
    await _deliver(harness, driver, 502)
    await harness.send(driver.tap("staff_sp_10_p1", message_id=SECOND_MESSAGE))
    armed = dict(_pending(harness))
    assert (armed["case_id"], armed["mode"], armed["message_id"]) == (10, "note", SECOND_MESSAGE)

    await harness.send(driver.tap(stale_tap, message_id=FIRST_MESSAGE))

    assert _pending(harness) == armed
    await harness.send(driver.text(NOTE))
    assert [call.data for call in backend_calls(harness, "PUT", _note_url(10))] == [{"note": NOTE}]
    assert backend_calls(harness, "PUT", _note_url(9)) == []
    assert harness.telegram.last_shown().text == THANKS
    assert not errors


async def test_note_answered_after_a_newer_case_was_armed_keeps_the_newer_prompt(monkeypatch):
    """The note step ends only the case it answered: when the prompt armed by the time the
    note's PUT returns is a newer case's, that prompt stays armed."""
    harness, driver, _labels = await _picked(monkeypatch)
    newer = {"case_id": 10, "mode": "note", "chat_id": DEFAULT_DRIVER_TELEGRAM_ID, "message_id": SECOND_MESSAGE}

    def _note_while_newer_case_is_armed(_call):
        harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["pending_shared_place_note"] = dict(newer)
        return {"case_id": 9, "note_saved": True}

    harness.backend.route("PUT", _note_url(9), _note_while_newer_case_is_armed)

    await harness.send(driver.text(NOTE))

    assert harness.telegram.last_shown().text == THANKS
    assert _pending(harness) == newer
