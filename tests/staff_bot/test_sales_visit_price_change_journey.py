"""The sales agent's order confirm card during an announced price change.

The agent reads the card out to the shopkeeper, so it must state what the
server quoted: the kept-price split under the line, the outlet's courtesy
status under the total, and the dated coming price while the notice runs.
Every quote is a literal for its basket; the bot's output is compared with it.
"""
import pytest

from staff_bot.handlers.sales.visit import V_CONFIRM

from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import _agent, _alerts, _curated, _rendered
from tests.staff_bot.test_sales_visit_orders_journey import (
    CONV, ESTIMATE, NAMES, ORDER, _calls, _estimate_body, _freeze_today, _order_response, _to_order,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

UZS = _curated("staff.currency.uzs")

# 20 bottles, the first 2 at the kept 18,000, the other 18 at the regular 20,000.
KEPT_ROW = {
    "product_id": 3, "product_name": NAMES[3], "quantity": 20,
    "unit_price": 20000.0, "total_price": 396000.0, "is_contract_price": False,
    "courtesy_quantity": 2, "courtesy_unit_price": 18000.0, "regular_quantity": 18,
    "upcoming_price": None,
}
STATUS_AVAILABLE = {
    "state": "available", "expires_on": "2027-02-14",
    "caps": [{"product_id": 3, "product_name": NAMES[3], "cap": 4}],
    "used_order_id": None, "used_order_number": None,
}


def _estimate(row, status):
    return {"client_id": 41, "currency": "UZS", "items": [row], "subtotal": row["total_price"],
            "delivery_fee": 0.0, "discount_amount": 0.0, "loyalty_discount": 0.0,
            "tier_discount": 0.0, "total_amount": row["total_price"], "courtesy_status": status}


async def _to_confirm(monkeypatch, estimate):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE, lambda c: {"estimate": estimate})
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    return harness.telegram.last_shown().text.split("\n")


async def test_the_confirm_card_shows_the_kept_price_split_and_the_outlets_status(monkeypatch):
    lines = await _to_confirm(monkeypatch, _estimate(KEPT_ROW, STATUS_AVAILABLE))

    split = _rendered(
        "staff.price_change.courtesy_split", courtesy_qty=2, courtesy_price=f"18,000 {UZS}",
        regular_qty=18, regular_price=f"20,000 {UZS}")
    status = "🏷 " + _rendered("staff.price_change.status_available", date="14.02.2027")
    cap = "   • " + _rendered("staff.price_change.status_cap_line", product=NAMES[3], cap=4)
    start = lines.index(f"• {NAMES[3]} × 20")
    assert lines[start:start + 4] == [f"• {NAMES[3]} × 20", split, f"💰 396,000 {UZS}", status]
    assert lines[start + 4] == cap


async def test_the_confirm_card_states_the_coming_price_during_the_notice(monkeypatch):
    row = {**KEPT_ROW, "courtesy_quantity": None, "courtesy_unit_price": None, "regular_quantity": None,
           "total_price": 400000.0,
           "upcoming_price": {"new_price": 22000.0, "effective_date": "2027-02-01"}}
    status = {"state": "none", "expires_on": None, "caps": [], "used_order_id": None,
              "used_order_number": None}
    lines = await _to_confirm(monkeypatch, _estimate(row, status))

    coming = "📅 " + _rendered("staff.price_change.upcoming_line", date="01.02.2027", price=f"22,000 {UZS}")
    start = lines.index(f"• {NAMES[3]} × 20")
    assert lines[start:start + 3] == [f"• {NAMES[3]} × 20", coming, f"💰 400,000 {UZS}"]
    assert not any(line.startswith("🏷") for line in lines)


async def test_the_agent_order_carries_the_total_the_confirm_card_showed(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE, lambda c: {"estimate": _estimate(KEPT_ROW, STATUS_AVAILABLE)})
    harness.backend.route("POST", ORDER, lambda c: _order_response())
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert [c.data["expected_total"] for c in _calls(harness, "POST", ORDER)] == [396000.0]


async def test_a_price_change_at_send_redraws_the_confirm_card_with_the_new_total(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE, lambda c: {"estimate": _estimate(KEPT_ROW, STATUS_AVAILABLE)})
    new_row = {**KEPT_ROW, "courtesy_quantity": None, "courtesy_unit_price": None,
               "regular_quantity": None, "total_price": 400000.0}
    new_quote = _estimate(new_row, {"state": "used", "expires_on": "2027-02-14", "caps": [],
                                    "used_order_id": 91, "used_order_number": "BS-270203-0007"})
    answers = [
        staff_backend_failure(
            "api.common.state_changed", status_code=409, error_code="STATE_CHANGED",
            details={"reason": "price_changed", "quote": new_quote}, error_type="CONFLICT"),
        _order_response(),
    ]
    harness.backend.route("POST", ORDER, lambda c: answers.pop(0))
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    card = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == V_CONFIRM
    assert card.text.split("\n")[0] == _rendered("staff.price_change.price_changed_note")
    assert f"💰 400,000 {UZS}" in card.text
    assert not any(_rendered("staff.error.api.conflict") in alert for alert in _alerts(harness))

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert [c.data["expected_total"] for c in _calls(harness, "POST", ORDER)] == [396000.0, 400000.0]
