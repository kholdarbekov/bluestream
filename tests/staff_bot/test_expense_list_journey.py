"""My expenses and Withdraw (Task 23), driven through the real PTB dispatcher.

Each journey taps the real buttons and asserts the exact request the bot sends.
"""
import pytest

from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES, DRIVER_EXPENSE_STATUSES
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.utils.formatters import format_currency
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_bottle_flows_dispatcher import (
    _curated,
    _translation_table,
    backend_calls as _calls,
    build_driver,
    open_cash_hub,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OPTIONS = "/api/v1/staff/expenses/options"
EXPENSES = "/api/v1/staff/expenses"
WITHDRAW_7 = "/api/v1/staff/expenses/7/withdraw"

NEEDED = (
    "staff.menu.add_expense",
    "staff.menu.my_expenses",
    "staff.expense.my_expenses",
    "staff.expense.waiting_header",
    "staff.expense.decided_header",
    "staff.expense.none",
    "staff.expense.withdraw",
    "staff.expense.withdraw_confirm",
    "staff.expense.withdrawn",
    "staff.expense.new_title",
    "staff.expense.cash_held_now",
    "staff.expense.pick_category",
    "staff.expense.enter_amount",
    "staff.expense.add_note_or_photo",
    "staff.expense.note_label",
    "staff.expense.receipt_attached",
    "staff.expense.send",
    "staff.expense.sent",
    "staff.expense.sent_explainer",
    "staff.expense.hand_in_now_waiting",
    "staff.error.api.expense_not_withdrawable",
    "staff.cancel",
    "staff.yes",
    "staff.no",
    "staff.currency.uzs",
    "staff.back",
    *(f"staff.expense.category.{category}" for category in DRIVER_EXPENSE_CATEGORIES),
    *(f"staff.expense.status.{status}" for status in DRIVER_EXPENSE_STATUSES),
)


def _row(expense_id, category, amount, status, approved_amount=None):
    return {
        "id": expense_id, "category": category, "amount": amount, "approved_amount": approved_amount,
        "note": None, "has_receipt": False, "status": status, "submitted_at": "2026-11-18T08:00:00+00:00",
        "decided_at": None, "decision_reason": None, "driver_cash_session_id": 124,
    }


_LIST = {
    "waiting": [_row(7, "fuel", 150000.0, "pending")],
    "decided": [
        _row(5, "caps", 40000.0, "approved", approved_amount=30000.0),
        _row(4, "labels", 20000.0, "rejected"),
        _row(3, "vehicle", 90000.0, "withdrawn"),
    ],
    "hand_in_now": 270000.0,
    "expenses_waiting_total": 150000.0,
}
_EMPTY = {"waiting": [], "decided": [], "hand_in_now": 0.0, "expenses_waiting_total": 0.0}
_WITHDRAWN = {
    "expense": _row(7, "fuel", 150000.0, "withdrawn"),
    "replayed": False,
    "hand_in_now": 420000.0,
    "expenses_waiting_total": 0.0,
}
_CREATED = {
    "expense": _row(7, "fuel", 150000.0, "pending"),
    "replayed": False,
    "hand_in_now": 270000.0,
    "expenses_waiting_total": 150000.0,
}


@pytest.fixture
async def driver(monkeypatch):
    table = _translation_table({
        (language, key): _curated(key, language)
        for key in NEEDED
        for language in ("en", "uz", "ru")
    })
    harness = await build_driver(monkeypatch, translations=table)
    harness.backend.route("GET", EXPENSES, lambda _call: _LIST)
    harness.backend.route("GET", OPTIONS, lambda _call: {
        "has_active_trip": True, "session_id": 124, "max_amount": 420000.0, "expenses_waiting_total": 0.0,
    })
    harness.backend.route("POST", EXPENSES, lambda _call: _CREATED)
    harness.backend.route("POST", WITHDRAW_7, lambda _call: _WITHDRAWN)
    return harness


async def test_my_expenses_lists_waiting_with_withdraw_and_the_decided(driver):
    updates, labels = await sign_in(driver)
    await open_cash_hub(driver, updates, labels)

    await driver.send(updates.tap("staff_expense_list"))

    shown = driver.telegram.last_shown()
    assert len(_calls(driver, "GET", EXPENSES)) == 1
    assert "staff_expense_withdraw_7" in shown.callback_data()
    assert "staff_expense_withdraw_4" not in shown.callback_data()
    text = shown.text
    assert _curated("staff.expense.my_expenses") in text
    assert text.index(_curated("staff.expense.waiting_header")) < text.index(_curated("staff.expense.decided_header"))
    assert _curated("staff.expense.category.fuel") in text
    for status in ("approved", "rejected", "withdrawn"):
        assert _curated(f"staff.expense.status.{status}") in text
    # The approved claim paid less than asked: both figures show.
    assert f"{format_currency(30000.0)} / {format_currency(40000.0)}" in text
    assert _curated("staff.expense.category.caps") in text
    assert "staff_cash_hub" in shown.callback_data()


async def test_withdraw_asks_to_confirm_before_it_posts(driver):
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_expense_list"))

    await driver.send(updates.tap("staff_expense_withdraw_7"))

    asked = driver.telegram.last_shown()
    assert _calls(driver, "POST", WITHDRAW_7) == []
    assert "staff_expense_withdraw_yes_7" in asked.callback_data()
    assert _curated("staff.expense.category.fuel") in asked.text

    await driver.send(updates.tap("staff_expense_withdraw_yes_7"))

    posts = _calls(driver, "POST", WITHDRAW_7)
    assert len(posts) == 1
    done = driver.telegram.last_shown().text
    assert done == _curated("staff.expense.withdrawn").format(amount=format_currency(420000.0))


async def test_a_decided_claim_explains_why_it_cannot_be_withdrawn(driver):
    driver.backend.route("POST", WITHDRAW_7, lambda _call: staff_backend_failure(
        "decided", 409, "DRIVER_EXPENSE_NOT_WITHDRAWABLE",
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_expense_list"))
    await driver.send(updates.tap("staff_expense_withdraw_7"))
    driver.telegram.reset()

    await driver.send(updates.tap("staff_expense_withdraw_yes_7"))

    assert len(_calls(driver, "POST", WITHDRAW_7)) == 1
    assert _curated("staff.error.api.expense_not_withdrawable") in driver.telegram.last_shown().text


async def test_the_cash_hub_always_offers_my_expenses(driver):
    driver.backend.route("GET", EXPENSES, lambda _call: _EMPTY)
    updates, labels = await sign_in(driver)
    hub = await open_cash_hub(driver, updates, labels)
    assert hub.index("staff_expense_start") + 1 == hub.index("staff_expense_list")
    assert hub[hub.index("staff_expense_list") + 1] == "staff_back_to_main"

    await driver.send(updates.tap("staff_expense_list"))

    assert _curated("staff.expense.none") in driver.telegram.last_shown().text


async def test_the_cash_hub_button_is_labelled_my_expenses(driver):
    updates, labels = await sign_in(driver)
    await driver.send(updates.text(next(label for label in labels if _curated("staff.menu.cash") in label)))
    hub = driver.telegram.last_shown()
    assert f"🧾 {_curated('staff.menu.my_expenses')}" in hub.button_labels()


async def test_a_waiting_claim_is_withdrawable_from_the_cash_hub_after_the_sent_message_is_gone(driver):
    updates, labels = await sign_in(driver)
    hub = await open_cash_hub(driver, updates, labels)
    assert "staff_expense_start" in hub
    await driver.send(updates.tap("staff_expense_start"))
    await driver.send(updates.tap("staff_expense_cat_fuel"))
    await driver.send(updates.text("150000"))
    await driver.send(updates.tap("staff_expense_send"))
    assert len(_calls(driver, "POST", EXPENSES)) == 1
    driver.telegram.reset()

    # The sent message is gone: the driver opens the Cash hub again, a new message.
    await open_cash_hub(driver, updates, labels)
    await driver.send(updates.tap("staff_expense_list"))
    assert "staff_expense_withdraw_7" in driver.telegram.last_shown().callback_data()
    await driver.send(updates.tap("staff_expense_withdraw_7"))
    await driver.send(updates.tap("staff_expense_withdraw_yes_7"))

    assert len(_calls(driver, "POST", WITHDRAW_7)) == 1


def test_the_sent_screen_offers_my_expenses(monkeypatch):
    class _I18n:
        @staticmethod
        def get(key, language, **kwargs):
            return key

    monkeypatch.setattr("staff_bot.keyboards.delivery.i18n", _I18n)
    keyboard = DeliveryKeyboards.expense_sent("en")
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert data == ["staff_expense_list", "staff_cash_hub"]
