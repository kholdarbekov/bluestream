"""Add expense: the driver's claim conversation (Task 22), driven through the real PTB dispatcher.

Each journey taps the real buttons, types real text and asserts the exact body the bot posts.
"""
import re

import pytest

from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES
from staff_bot.api_client import TRANSPORT_AMBIGUOUS_ERROR_CODE
from staff_bot.handlers.delivery.expenses import (
    EXPENSE_AMOUNT,
    EXPENSE_CATEGORY,
    EXPENSE_CONFIRM,
    EXPENSE_FLOW_KEY,
)
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.utils import flow_state
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_bottle_flows_dispatcher import (
    _curated,
    _translation_table,
    backend_calls as _calls,
    build_driver,
    menu_label,
    open_cash_hub,
    sign_in,
    user_data,
)
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OPTIONS = "/api/v1/staff/expenses/options"
EXPENSES = "/api/v1/staff/expenses"
CONVERSATION = "staff_expense"

NEEDED = (
    "staff.menu.add_expense",
    "staff.expense.new_title",
    "staff.expense.cash_held_now",
    "staff.expense.pick_category",
    "staff.expense.enter_amount",
    "staff.expense.above_cash_held",
    "staff.expense.no_trip_cash",
    "staff.expense.add_note_or_photo",
    "staff.expense.note_label",
    "staff.expense.receipt_attached",
    "staff.expense.send",
    "staff.expense.sent",
    "staff.expense.sent_explainer",
    "staff.expense.hand_in_now_waiting",
    "staff.error.api.expense_note_required",
    "staff.error.api.expense_above_cash_held",
    "staff.delivery.invalid_amount",
    "staff.cancelled",
    "staff.cancel",
    "staff.flow_timed_out",
    "staff.currency.uzs",
    "staff.back",
    *(f"staff.expense.category.{category}" for category in DRIVER_EXPENSE_CATEGORIES),
)


# `DriverExpenseClaims.answer`: the recorded claim and the trip's figures after it.
_CREATED = {
    "expense": {"id": 7, "status": "pending"},
    "replayed": False,
    "hand_in_now": 270000.0,
    "expenses_waiting_total": 150000.0,
}


def _options(**overrides) -> dict:
    options = {
        "has_active_trip": True,
        "session_id": 124,
        "max_amount": 420000.0,
        "expenses_waiting_total": 0.0,
    }
    options.update(overrides)
    return options


@pytest.fixture
async def driver(monkeypatch):
    table = _translation_table({
        (language, key): _curated(key, language)
        for key in NEEDED
        for language in ("en", "uz", "ru")
    })
    harness = await build_driver(monkeypatch, translations=table)
    harness.backend.route("GET", OPTIONS, lambda _call: _options())
    harness.backend.route("POST", EXPENSES, lambda _call: _CREATED)
    return harness


async def _start(driver):
    """Sign in, open the Cash hub and tap Add expense; return the update factory and the menu labels."""
    updates, labels = await sign_in(driver)
    hub = await open_cash_hub(driver, updates, labels)
    assert "staff_expense_start" in hub
    await driver.send(updates.tap("staff_expense_start"))
    return updates, labels


async def _to_confirm(driver, updates, category="fuel", amount="80000"):
    await driver.send(updates.tap(f"staff_expense_cat_{category}"))
    await driver.send(updates.text(amount))


def _body(driver, index=0) -> dict:
    return _calls(driver, "POST", EXPENSES)[index].data


def _draft(driver) -> dict:
    return user_data(driver)[EXPENSE_FLOW_KEY]


async def test_the_full_flow_posts_the_exact_body(driver):
    updates, _labels = await _start(driver)
    assert driver.conversation_state(CONVERSATION) == EXPENSE_CATEGORY
    categories = driver.telegram.last_shown().callback_data()
    assert categories == [
        *(f"staff_expense_cat_{category}" for category in DRIVER_EXPENSE_CATEGORIES),
        "staff_expense_cancel",
    ]
    assert _calls(driver, "GET", OPTIONS)

    await driver.send(updates.tap("staff_expense_cat_fuel"))
    assert driver.conversation_state(CONVERSATION) == EXPENSE_AMOUNT
    await driver.send(updates.text("150 000"))
    assert driver.conversation_state(CONVERSATION) == EXPENSE_CONFIRM
    await driver.send(updates.photo(caption="Lukoil", file_id="AgAC-big"))
    assert driver.conversation_state(CONVERSATION) == EXPENSE_CONFIRM
    confirm = driver.telegram.last_shown()
    assert "staff_expense_send" in confirm.callback_data()
    assert _curated("staff.expense.receipt_attached") in confirm.text
    assert "Lukoil" in confirm.text

    await driver.send(updates.tap("staff_expense_send"))

    assert len(_calls(driver, "POST", EXPENSES)) == 1
    body = _body(driver)
    assert {k: body[k] for k in ("category", "amount", "note", "receipt_file_id")} == {
        "category": "fuel", "amount": 150000.0, "note": "Lukoil", "receipt_file_id": "AgAC-big",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", body["idempotency_key"])
    assert set(body) == {"category", "amount", "note", "receipt_file_id", "idempotency_key"}
    sent = driver.telegram.last_shown()
    uzs = _curated("staff.currency.uzs")
    assert _curated("staff.expense.sent", "en").format(
        category=_curated("staff.expense.category.fuel"), amount=f"150,000 {uzs}"
    ) in sent.text
    assert _curated("staff.expense.sent_explainer") in sent.text
    assert _curated("staff.expense.hand_in_now_waiting").format(
        amount=f"270,000 {uzs}", waiting=f"150,000 {uzs}"
    ) in sent.text
    assert sent.callback_data() == ["staff_expense_list", "staff_cash_hub"]
    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert driver.conversation_state(CONVERSATION) is None


async def test_a_claim_without_a_receipt_or_note_is_sent(driver):
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates, "fuel", "80000")
    assert "staff_expense_send" in driver.telegram.last_shown().callback_data()

    await driver.send(updates.tap("staff_expense_send"))

    body = _body(driver)
    assert {k: body[k] for k in ("category", "amount", "note", "receipt_file_id")} == {
        "category": "fuel", "amount": 80000.0, "note": None, "receipt_file_id": None,
    }


async def test_an_amount_above_the_published_maximum_is_refused_on_the_spot(driver):
    updates, _labels = await _start(driver)
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    driver.telegram.reset()

    await driver.send(updates.text("420 001"))

    refusal = driver.telegram.last_shown().text
    assert _curated("staff.expense.above_cash_held").format(amount=f"420,000 {_curated('staff.currency.uzs')}") in refusal
    assert driver.conversation_state(CONVERSATION) == EXPENSE_AMOUNT
    assert _calls(driver, "POST", EXPENSES) == []

    await driver.send(updates.text("420000"))
    assert driver.conversation_state(CONVERSATION) == EXPENSE_CONFIRM


@pytest.mark.parametrize("typed", ["abc", "0", "-5", " "])
async def test_a_bad_amount_is_refused_and_asked_again(driver, typed):
    updates, _labels = await _start(driver)
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    driver.telegram.reset()

    await driver.send(updates.text(typed))

    assert _curated("staff.delivery.invalid_amount") in driver.telegram.last_shown().text
    assert driver.conversation_state(CONVERSATION) == EXPENSE_AMOUNT
    assert _calls(driver, "POST", EXPENSES) == []


async def test_the_expense_amount_reads_150_dot_000_as_150000(driver):
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates, "caps", "150.000")

    assert _draft(driver)["amount"] == 150000.0
    await driver.send(updates.tap("staff_expense_send"))
    assert _body(driver)["amount"] == 150000.0


async def test_other_needs_a_note_before_send(driver):
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates, "other", "30000")

    confirm = driver.telegram.last_shown()
    assert "staff_expense_send" not in confirm.callback_data()
    assert "staff_expense_cancel" in confirm.callback_data()
    assert _curated("staff.error.api.expense_note_required") in confirm.text

    # A photo without a caption does not satisfy it.
    await driver.send(updates.photo(file_id="AgAC-nocaption"))
    assert "staff_expense_send" not in driver.telegram.last_shown().callback_data()
    assert _calls(driver, "POST", EXPENSES) == []

    await driver.send(updates.text("Padlock for the van"))
    assert "staff_expense_send" in driver.telegram.last_shown().callback_data()

    await driver.send(updates.tap("staff_expense_send"))
    body = _body(driver)
    assert {k: body[k] for k in ("category", "amount", "note", "receipt_file_id")} == {
        "category": "other", "amount": 30000.0, "note": "Padlock for the van",
        "receipt_file_id": "AgAC-nocaption",
    }


async def test_a_menu_tap_clears_the_draft(driver):
    updates, labels = await _start(driver)
    await _to_confirm(driver, updates)
    assert EXPENSE_FLOW_KEY in user_data(driver)

    await driver.send(updates.text(menu_label(labels, "staff.menu.cash")))

    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert driver.conversation_state(CONVERSATION) is None


async def test_a_timeout_clears_the_draft(driver):
    updates, labels = await _start(driver)
    last = updates.text("80000")
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    await driver.send(last)
    driver.telegram.reset()

    await fire_timeout(driver, CONVERSATION, last)

    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert driver.telegram.last_shown().text == _curated("staff.flow_timed_out")
    assert driver.telegram.last_shown().button_labels() == labels


@pytest.mark.parametrize("options", [
    _options(has_active_trip=False, session_id=None, max_amount=0.0),
    _options(max_amount=0.0),
])
async def test_no_trip_cash_does_not_start_the_flow(driver, options):
    driver.backend.route("GET", OPTIONS, lambda _call: options)

    await _start(driver)

    assert _curated("staff.expense.no_trip_cash") in driver.telegram.last_shown().text
    assert driver.conversation_state(CONVERSATION) is None
    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert _calls(driver, "POST", EXPENSES) == []


async def test_a_changed_draft_mints_a_new_key_and_a_resend_reuses_it(driver):
    answers = iter([
        staff_backend_failure("timed out", None, TRANSPORT_AMBIGUOUS_ERROR_CODE),
        staff_backend_failure("timed out", None, TRANSPORT_AMBIGUOUS_ERROR_CODE),
        _CREATED,
    ])
    driver.backend.route("POST", EXPENSES, lambda _call: next(answers))
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates)

    await driver.send(updates.tap("staff_expense_send"))
    assert driver.conversation_state(CONVERSATION) == EXPENSE_CONFIRM
    await driver.send(updates.tap("staff_expense_send"))
    first, second = _body(driver, 0), _body(driver, 1)
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first == second

    await driver.send(updates.text("Lukoil on Chilonzor"))
    await driver.send(updates.tap("staff_expense_send"))

    third = _body(driver, 2)
    assert third["idempotency_key"] != first["idempotency_key"]
    assert re.fullmatch(r"[0-9a-f]{32}", third["idempotency_key"])
    assert third["note"] == "Lukoil on Chilonzor"
    assert driver.conversation_state(CONVERSATION) is None


async def test_a_new_photo_mints_a_new_key(driver):
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates)
    key = _draft(driver)["idempotency_key"]
    assert re.fullmatch(r"[0-9a-f]{32}", key)

    await driver.send(updates.photo(file_id="AgAC-new"))

    assert _draft(driver)["receipt_file_id"] == "AgAC-new"
    assert _draft(driver)["idempotency_key"] != key


async def test_a_refusal_keeps_the_draft_on_the_confirm_screen(driver):
    driver.backend.route("POST", EXPENSES, lambda _call: staff_backend_failure(
        "too much", 409, "DRIVER_EXPENSE_ABOVE_CASH_HELD",
    ))
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates)
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_send"))

    assert driver.conversation_state(CONVERSATION) == EXPENSE_CONFIRM
    assert _draft(driver)["amount"] == 80000.0
    assert _curated("staff.error.api.expense_above_cash_held") in driver.telegram.last_shown().text


async def test_an_old_send_after_a_timeout_says_the_draft_ended_and_shows_the_cash_hub(driver):
    updates, _labels = await _start(driver)
    last = updates.text("80000")
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    await driver.send(last)
    await fire_timeout(driver, CONVERSATION, last)
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_send"))

    answers = driver.telegram.of("answerCallbackQuery")
    assert [a.params.get("text") for a in answers] == [_curated("staff.cancelled")]
    shown = driver.telegram.last_shown()
    assert shown.text == _curated("staff.cancelled")
    assert "staff_expense_start" in shown.callback_data()
    assert _calls(driver, "POST", EXPENSES) == []
    assert driver.conversation_state(CONVERSATION) is None


async def test_an_old_cancel_after_a_timeout_is_answered_and_shows_the_cash_hub(driver):
    """Every note or photo draws a new confirm screen, so older ones keep their Cancel after the draft ends."""
    updates, _labels = await _start(driver)
    last = updates.text("80000")
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    await driver.send(last)
    await fire_timeout(driver, CONVERSATION, last)
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_cancel"))

    assert len(driver.telegram.visible_answers) == 1
    shown = driver.telegram.last_shown()
    assert shown.text == _curated("staff.cancelled")
    assert "staff_expense_start" in shown.callback_data()
    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert _calls(driver, "POST", EXPENSES) == []
    assert driver.conversation_state(CONVERSATION) is None


async def test_an_old_category_tap_after_a_menu_tap_answers_the_same_way(driver):
    updates, labels = await _start(driver)
    await driver.send(updates.text(menu_label(labels, "staff.menu.cash")))
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_cat_fuel"))

    answers = driver.telegram.of("answerCallbackQuery")
    assert [a.params.get("text") for a in answers] == [_curated("staff.cancelled")]
    shown = driver.telegram.last_shown()
    assert shown.text == _curated("staff.cancelled")
    assert "staff_expense_start" in shown.callback_data()
    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert driver.conversation_state(CONVERSATION) is None


async def test_an_old_send_on_a_draft_without_an_amount_is_refused(driver):
    updates, _labels = await _start(driver)
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_send"))

    assert driver.telegram.last_shown().text == _curated("staff.cancelled")
    assert _calls(driver, "POST", EXPENSES) == []


async def test_cancel_drops_the_draft_and_shows_the_cash_hub(driver):
    updates, _labels = await _start(driver)
    await _to_confirm(driver, updates)

    await driver.send(updates.tap("staff_expense_cancel"))

    assert EXPENSE_FLOW_KEY not in user_data(driver)
    assert driver.conversation_state(CONVERSATION) is None
    assert _calls(driver, "POST", EXPENSES) == []


def test_the_draft_key_is_registered_with_the_flow_state_sweep():
    assert EXPENSE_FLOW_KEY == "pending_expense_draft"
    assert EXPENSE_FLOW_KEY in flow_state.PENDING_FLOW_USER_DATA_KEYS


def test_the_category_emoji_cover_exactly_the_shared_categories():
    assert tuple(DeliveryKeyboards.EXPENSE_CATEGORY_EMOJI) == DRIVER_EXPENSE_CATEGORIES
    assert tuple(DeliveryKeyboards.EXPENSE_CATEGORY_EMOJI.values()) == (
        "⛽", "🧴", "🔘", "🏷", "📦", "🔧", "🅿️", "✏️",
    )


def test_the_confirm_keyboard_draws_send_only_when_it_can_be_sent():
    def data(can_send):
        return [b.callback_data for row in DeliveryKeyboards.expense_confirm("en", can_send).inline_keyboard for b in row]

    assert data(True) == ["staff_expense_send", "staff_expense_cancel"]
    assert data(False) == ["staff_expense_cancel"]
    assert [b.callback_data for row in DeliveryKeyboards.expense_sent("en").inline_keyboard for b in row] == [
        "staff_expense_list", "staff_cash_hub"
    ]
