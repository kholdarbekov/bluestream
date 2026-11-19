"""One money-typing rule at every point a driver types an amount (Task 20).

"150.000" is one hundred fifty thousand soum, not 150. Each journey runs through
the real PTB dispatcher and asserts the body the bot posts to the backend.
The at-door cash point is driven in test_staff_delivery_journey_dispatcher.py's
world; here the typed hand-in, the debt collection and the bottle fine are
driven from the cash hub's own callbacks.
"""
import pytest

from tests.staff_bot.test_staff_bottle_flows_dispatcher import (
    ADDRESS_ID,
    CUSTOMER_ID,
    SESSION_CURRENT,
    _curated,
    _translation_table,
    backend_calls,
    build_driver,
    sign_in,
)
from tests.staff_bot import test_staff_delivery_journey_dispatcher as door

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUBMIT = "/api/v1/staff/reconciliation/session/submit"
COLLECTIONS = "/api/v1/staff/cash-collections"
COD_STATEMENT = f"/api/v1/staff/customers/{CUSTOMER_ID}/cod-statement"
FINE = "/api/v1/staff/bottles/fine"

NEEDED = (
    "staff.delivery.invalid_amount",
    "staff.delivery.invalid_cash_amount",
    "staff.delivery.enter_declared_cash",
    "staff.delivery.reconciliation_submitted",
    "staff.delivery.enter_fine_bottle_qty",
    "staff.delivery.enter_fine_amount",
    "staff.delivery.enter_fine_note",
    "staff.delivery.cod_collection_amount_prompt",
    "staff.delivery.cod_collection_note_prompt",
)


@pytest.fixture
async def driver(monkeypatch):
    table = _translation_table({
        (language, key): _curated(key, language)
        for key in NEEDED
        for language in ("en", "uz", "ru")
    })
    harness = await build_driver(monkeypatch, translations=table)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: None)
    return harness


async def test_a_typed_hand_in_of_150_dot_000_posts_150000(driver):
    driver.backend.route("POST", SUBMIT, lambda _call: {"status": "submitted"})
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap("staff_reconcile_submit"))
    await driver.send(updates.text("150.000"))

    posts = backend_calls(driver, "POST", SUBMIT)
    assert [call.data for call in posts] == [{"declared_cash": 150000.0}]


async def test_a_typed_hand_in_of_1500_dot_000_is_refused_and_posts_nothing(driver):
    updates, _labels = await sign_in(driver)
    await driver.send(updates.tap("staff_reconcile_submit"))

    driver.telegram.reset()
    await driver.send(updates.text("1500.000"))

    assert [shown.text for shown in driver.telegram.shown] == [
        _curated("staff.delivery.invalid_amount")
    ]
    assert backend_calls(driver, "POST", SUBMIT) == []


async def test_a_debt_collection_of_150_dot_000_posts_150000(driver):
    driver.backend.route("GET", COD_STATEMENT, lambda _call: {
        "total_outstanding_amount": 0, "items": [], "first_name": "Kamola",
        "phone": "+998901234567",
    })
    driver.backend.route("POST", COLLECTIONS, lambda _call: {"id": 1})
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap(f"staff_cod_collect_custom_{CUSTOMER_ID}"))
    await driver.send(updates.text("150.000"))
    await driver.send(updates.text("paid at the shop"))

    posts = backend_calls(driver, "POST", COLLECTIONS)
    assert len(posts) == 1
    assert posts[0].data["amount"] == 150000.0
    assert posts[0].data["customer_id"] == CUSTOMER_ID


async def test_a_bottle_fine_of_150_dot_000_posts_150000(driver):
    driver.backend.route("POST", FINE, lambda _call: {"id": 1})
    updates, _labels = await sign_in(driver)

    await driver.send(updates.tap(f"staff_bottle_fine_{CUSTOMER_ID}_{ADDRESS_ID}"))
    await driver.send(updates.text("1"))
    await driver.send(updates.text("150.000"))
    await driver.send(updates.text("bottle left broken"))

    posts = backend_calls(driver, "POST", FINE)
    assert len(posts) == 1
    assert posts[0].data["fine_amount"] == 150000.0
    assert posts[0].data["quantity"] == 1


async def test_an_at_door_cash_of_150_dot_000_posts_150000(monkeypatch):
    harness = await door.build_driver(monkeypatch)
    harness.desk.active = [door.active_row(501, "BS-1001")]
    harness.desk.serve(501)
    updates, _detail = await door.open_the_stop(harness)
    await door.walk_to_the_door(harness, updates)
    await harness.send(updates.tap("staff_status_501_delivered"))
    await harness.send(updates.tap("staff_cash_partial_501"))
    await harness.send(updates.text("150.000"))
    await harness.send(updates.text("Counted at the door"))

    assert door.status_writes(harness)[-1] == (
        door.status_endpoint(501),
        {"status": "delivered", "metadata": {"cash_collected": 150000.0, "notes": "Counted at the door"}},
    )
