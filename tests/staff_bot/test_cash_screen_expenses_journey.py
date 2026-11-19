"""The hand-in button names its trip and basis; the Cash screen shows expenses (Task 21).

Each journey runs through the real PTB dispatcher and asserts the exact body the bot posts.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from staff_bot.keyboards.delivery import DeliveryKeyboards, HandoffTap
from staff_bot.utils.price_lines import price_changed_quote
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_bottle_flows_dispatcher import (
    _curated,
    _translation_table,
    backend_calls,
    build_driver,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SESSION = "/api/v1/staff/reconciliation/session"
SUBMIT = "/api/v1/staff/reconciliation/session/submit"

NEEDED = (
    "staff.menu.cash_reconciliation",
    "staff.delivery.current_status",
    "staff.delivery.expected_cash_label",
    "staff.delivery.expected_cash_on_hand_label",
    "staff.delivery.declared_cash_label",
    "staff.delivery.cash_variance_label",
    "staff.delivery.remaining_to_submit",
    "staff.delivery.handoff_remaining_cash",
    "staff.delivery.edit_reconciliation_cash",
    "staff.delivery.reconciliation_submitted",
    "staff.delivery.reconciliation_partial_recorded",
    "staff.delivery.expenses_approved_label",
    "staff.delivery.expenses_waiting_label",
    "staff.delivery.handed_in_waiting_title",
    "staff.delivery.enter_declared_cash",
    "staff.error.api.expenses_waiting_hand_in",
    "staff.error.api.nothing_to_reconcile",
    "staff.currency.uzs",
    "staff.back",
)


def _session(**overrides) -> dict:
    session = {
        "id": 124,
        "status": "partial",
        "expected_cash": 400000.0,
        "expected_cash_on_hand": 270000.0,
        "declared_cash": None,
        "declared_variance": None,
        "declared_variance_net": 0.0,
        "remaining_cash_to_submit": 270000.0,
        "expenses_waiting_total": 150000.0,
        "expenses_approved_total": 0.0,
        "next_actions": ["submit_reconciliation"],
        "risk_flags": [],
    }
    session.update(overrides)
    return session


@pytest.fixture
async def driver(monkeypatch):
    table = _translation_table({
        (language, key): _curated(key, language)
        for key in NEEDED
        for language in ("en", "uz", "ru")
    })
    harness = await build_driver(monkeypatch, translations=table)
    harness.backend.route("GET", SESSION, lambda _call: _session())
    return harness


def _hand_in_callback(shown) -> str:
    """The one hand-in button's callback data on the screen the bot drew."""
    found = [data for data in shown.callback_data() if str(data).startswith("staff_reconcile_submit_all")]
    assert len(found) == 1, f"expected one hand-in button, got {found}"
    return found[0]


def _button_for(**kwargs):
    markup = DeliveryKeyboards.reconciliation_actions("en", can_submit=True, **kwargs)
    return markup.inline_keyboard[0][0]


async def test_the_hand_in_button_carries_trip_and_basis_and_posts_them(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: _session(status="partial", declared_cash=270000.0))
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_session"))
    callback = _hand_in_callback(driver.telegram.last_shown())
    assert callback == "staff_reconcile_submit_all:270000.00:124:150000.00"

    await driver.send(updates.tap(callback))

    posts = backend_calls(driver, "POST", SUBMIT)
    assert [call.data for call in posts] == [
        {"declared_cash": 270000.0, "session_id": 124, "basis": 150000.0}
    ]
    assert isinstance(posts[0].data["session_id"], int)


def test_the_hand_in_button_fits_64_bytes_at_the_largest_figures():
    data = _button_for(
        remaining_amount=99999999.99, session_id=9999999, basis=99999999.99
    ).callback_data
    assert len(data.encode()) <= 64
    assert DeliveryKeyboards.parse_handoff_callback(data) == HandoffTap(
        Decimal("99999999.99"), 9999999, Decimal("99999999.99")
    )


def test_a_hand_in_button_without_a_trip_is_not_drawn():
    markup = DeliveryKeyboards.reconciliation_actions(
        "en", can_submit=True, remaining_amount=270000.0, basis=0
    )
    assert not [
        b for row in markup.inline_keyboard for b in row
        if (b.callback_data or "").startswith("staff_reconcile_submit_all")
    ]


@pytest.mark.parametrize("data", [
    "staff_reconcile_submit_all",
    "staff_reconcile_submit_all:",
    "staff_reconcile_submit_all:120000.00",
    "staff_reconcile_submit_all:120000.00:124",
    "staff_reconcile_submit_all:120000.00:124:0.00:9",
    "staff_reconcile_submit_all:abc:124:0.00",
    "staff_reconcile_submit_all:120000.00:x:0.00",
    "staff_reconcile_submit_all:120000.00:0:0.00",
    "staff_reconcile_submit_all:0.00:124:0.00",
    "staff_reconcile_submit_all:120000.00:124:-1.00",
    "staff_reconcile_submit_all:NaN:124:0.00",
])
def test_anything_but_four_good_fields_is_not_a_hand_in(data):
    assert DeliveryKeyboards.parse_handoff_callback(data) is None


async def test_an_amount_only_button_redraws_and_posts_nothing(driver):
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_submit_all:120000.00"))

    assert backend_calls(driver, "POST", SUBMIT) == []
    assert len(backend_calls(driver, "GET", SESSION)) == 1
    assert _hand_in_callback(driver.telegram.last_shown()) == (
        "staff_reconcile_submit_all:270000.00:124:150000.00"
    )


async def test_a_hand_in_changed_answer_redraws_the_cash_screen(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: staff_backend_failure(
        "claims moved", 409, "STATE_CHANGED", details={"reason": "hand_in_changed"}
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_session"))
    stale = _hand_in_callback(driver.telegram.last_shown())

    driver.backend.route("GET", SESSION, lambda _call: _session(
        remaining_cash_to_submit=420000.0, expenses_waiting_total=0.0
    ))
    driver.telegram.reset()
    await driver.send(updates.tap(stale))

    assert len(backend_calls(driver, "POST", SUBMIT)) == 1
    assert len(backend_calls(driver, "GET", SESSION)) == 2
    # Same trip, new figure: the tapped Cash screen is redrawn in place.
    assert [call.method for call in driver.telegram.shown] == ["editMessageText"]
    assert _hand_in_callback(driver.telegram.last_shown()) == (
        "staff_reconcile_submit_all:420000.00:124:0.00"
    )
    assert not [c for c in driver.telegram.of("answerCallbackQuery") if c.params.get("show_alert")]


async def test_a_button_for_a_trip_that_has_closed_sends_the_cash_screen_below_it(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: staff_backend_failure(
        "session changed", 409, "RECONCILIATION_SESSION_CHANGED"
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_session"))
    stale = _hand_in_callback(driver.telegram.last_shown())

    driver.backend.route("GET", SESSION, lambda _call: _session(id=125, remaining_cash_to_submit=90000.0))
    driver.telegram.reset()
    await driver.send(updates.tap(stale))

    assert _curated("staff.error.api.nothing_to_reconcile") not in " ".join(
        shown.text for shown in driver.telegram.shown
    )
    # The tapped message is left as it is; the current trip's Cash screen arrives as a new message.
    assert driver.telegram.of("editMessageText") == []
    assert [call.method for call in driver.telegram.shown] == ["sendMessage"]
    assert _hand_in_callback(driver.telegram.last_shown()) == (
        "staff_reconcile_submit_all:90000.00:125:150000.00"
    )
    assert len(driver.telegram.visible_answers) == 1


async def test_a_double_tap_on_a_hand_in_that_closes_the_trip_keeps_the_confirmation(driver):
    """Updates for one chat run in order, so tap 2 posts the closed trip's id and meets RECONCILIATION_SESSION_CHANGED
    after tap 1 has already drawn "✅ Handed in" on the message both taps came from."""
    answers = iter([
        _session(
            status="submitted", declared_cash=270000.0, remaining_cash_to_submit=0.0,
            expenses_waiting_total=0.0, next_actions=["await_admin_verification"],
        ),
        staff_backend_failure("session changed", 409, "RECONCILIATION_SESSION_CHANGED"),
    ])
    driver.backend.route("POST", SUBMIT, lambda _call: next(answers))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_session"))
    callback = _hand_in_callback(driver.telegram.last_shown())

    await driver.send(updates.tap(callback))
    confirmation = driver.telegram.last_shown()
    assert confirmation.method == "editMessageText"
    assert confirmation.text.startswith(f"✅ {_curated('staff.delivery.reconciliation_submitted')}")

    # The next trip has opened by the time tap 2 reads the Cash screen.
    driver.backend.route("GET", SESSION, lambda _call: _session(
        id=125, status="open", remaining_cash_to_submit=90000.0, expenses_waiting_total=0.0,
    ))
    driver.telegram.reset()
    await driver.send(updates.tap(callback))

    posts = backend_calls(driver, "POST", SUBMIT)
    assert [call.data for call in posts] == [
        {"declared_cash": 270000.0, "session_id": 124, "basis": 150000.0},
        {"declared_cash": 270000.0, "session_id": 124, "basis": 150000.0},
    ]
    assert driver.telegram.of("editMessageText") == []
    assert [call.method for call in driver.telegram.shown] == ["sendMessage"]
    assert _hand_in_callback(driver.telegram.last_shown()) == "staff_reconcile_submit_all:90000.00:125:0.00"
    assert len(driver.telegram.visible_answers) == 1


async def test_a_typed_hand_in_refused_for_waiting_claims_explains_and_redraws(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: staff_backend_failure(
        "waiting", 409, "RECONCILIATION_EXPENSES_WAITING",
        details={"hand_in_now": 270000.0, "expenses_waiting_total": 150000.0},
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_submit"))

    driver.telegram.reset()
    await driver.send(updates.text("400000"))

    texts = [shown.text for shown in driver.telegram.shown]
    assert _curated("staff.error.api.expenses_waiting_hand_in") in texts[0]
    assert _hand_in_callback(driver.telegram.last_shown()) == (
        "staff_reconcile_submit_all:270000.00:124:150000.00"
    )
    assert [call.data for call in backend_calls(driver, "POST", SUBMIT)] == [{"declared_cash": 400000.0}]


def test_a_hand_in_changed_answer_is_not_read_as_a_price_change():
    response = SimpleNamespace(status_code=409, error_code="STATE_CHANGED", details={"reason": "hand_in_changed"})
    assert price_changed_quote(response) is None


async def test_the_cash_screen_shows_expenses_and_the_net_variance(driver):
    driver.backend.route("GET", SESSION, lambda _call: _session(
        status="submitted", declared_cash=270000.0, declared_variance=-30000.0, declared_variance_net=0.0,
        expenses_approved_total=120000.0, expenses_waiting_total=30000.0,
        remaining_cash_to_submit=0.0, next_actions=["await_admin_verification"],
    ))
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_session"))

    text = driver.telegram.last_shown().text
    uzs = _curated("staff.currency.uzs")
    assert f"💵 {_curated('staff.delivery.expenses_approved_label')}: 120,000 {uzs}" in text
    assert f"⏳ {_curated('staff.delivery.expenses_waiting_label')}: 30,000 {uzs}" in text
    assert f"📌 {_curated('staff.delivery.remaining_to_submit')}: 0 {uzs}" in text
    assert f"⚠️ {_curated('staff.delivery.cash_variance_label')}: 0 {uzs}" in text
    assert "-30,000" not in text


async def test_the_cash_screen_without_expenses_prints_neither_expense_line(driver):
    driver.backend.route("GET", SESSION, lambda _call: _session(
        status="open", expenses_waiting_total=0.0, expenses_approved_total=0.0,
    ))
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_session"))

    text = driver.telegram.last_shown().text
    assert _curated("staff.delivery.expenses_approved_label") not in text
    assert _curated("staff.delivery.expenses_waiting_label") not in text
    assert _curated("staff.delivery.remaining_to_submit") not in text


async def test_while_only_the_office_can_act_there_is_no_hand_in_button(driver):
    driver.backend.route("GET", SESSION, lambda _call: _session(
        remaining_cash_to_submit=0.0, next_actions=["await_expense_decision"],
    ))
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_session"))

    callbacks = driver.telegram.last_shown().callback_data()
    assert not [data for data in callbacks if str(data).startswith("staff_reconcile_submit")]
    assert "staff_back_to_main" in callbacks


async def test_the_title_after_a_hand_in_names_the_waiting_amount(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: _session(
        status="partial", declared_cash=270000.0, remaining_cash_to_submit=0.0,
        next_actions=["await_expense_decision"],
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_session"))

    callback = _hand_in_callback(driver.telegram.last_shown())
    driver.telegram.reset()
    await driver.send(updates.tap(callback))

    text = driver.telegram.last_shown().text
    uzs = _curated("staff.currency.uzs")
    assert text.startswith(f"✅ Handed in. 150,000 {uzs} waiting for approval")
    assert _curated("staff.delivery.reconciliation_partial_recorded") not in text


async def test_the_title_after_a_hand_in_with_nothing_waiting_is_todays(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: _session(
        status="submitted", declared_cash=270000.0, remaining_cash_to_submit=0.0,
        expenses_waiting_total=0.0, next_actions=["await_admin_verification"],
    ))
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_session"))

    callback = _hand_in_callback(driver.telegram.last_shown())
    driver.telegram.reset()
    await driver.send(updates.tap(callback))

    assert driver.telegram.last_shown().text.startswith(
        f"✅ {_curated('staff.delivery.reconciliation_submitted')}"
    )
