"""The visit loop's second half, end to end through the real PTB Application.

What an agent does here MOVES MONEY: the order they send is created on the
customer's behalf and the outlet is billed for it. So every test below
asserts the exact JSON body that leaves the bot rather than that a call
happened — a basket that posts the wrong quantity, the wrong rail or
yesterday's date is the defect this file exists to catch, and all three look
identical to a call-occurrence assertion.

Three rules are pinned here because they are the ones a later refactor is
most likely to re-derive on the bot side:

* the payment step is SKIPPED when the backend offers one rail, and a rail it
  did not offer is refused even though the registered pattern is `\\w+`-wide;
* `delivery_date` is the agent's answer to a date question, never a
  server-side default (controller ruling 7), so "Tomorrow" is asserted
  against a FROZEN today rather than against whatever day the suite runs;
* the close outcome is the BACKEND's when an order exists — the bot posts
  `outcome: null` and never guesses `order_placed` for it.

Copy comes from `scripts/seed_staff_translations.py` through `_curated_value`,
the resolver `seed_translations()` itself calls, so a seed edit cannot leave
this file asserting strings production no longer ships.
"""
from datetime import date

import pytest

from staff_bot.handlers.sales import visit as visit_module
from staff_bot.handlers.sales.visit import (
    FLOW_KEY,
    V_CHECKIN,
    V_CLOSE,
    V_CONFIRM,
    V_DAY,
    V_NOTES,
    V_ORDER,
    V_ORDER_EDIT,
    V_PAYMENT,
    V_STOCK,
)
from staff_bot.api_client import TRANSPORT_AMBIGUOUS_ERROR_CODE
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import (
    OUTLETS, _agent, _alerts, _calls, _curated, _label, _rendered,
)
from tests.staff_bot.test_sales_visit_journey import (
    ACCURACY,
    CHECKIN,
    CONV,
    CURRENT,
    DOOR,
    SALES,
    STOCK_CHECK,
    _start,
    _stock_row,
    _to_stock,
    _visit,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAYMENTS = f"{OUTLETS}/7/payment-methods"
ESTIMATE = f"{OUTLETS}/7/order-estimate"
ORDER = f"{SALES}/visits/31/order"
CLOSE = f"{SALES}/visits/31/close"
ABANDON = f"{SALES}/visits/31/abandon"

# The two rails a sales visit may use; Click and Payme are filtered out
# backend-side and must never reach the keyboard.
CASH = {"method": "cash", "name": "Cash on Delivery", "description": "Pay when it arrives"}
BUSINESS = {"method": "business_account", "name": "Business account", "description": "Bill the contract"}

NAMES = {3: "Pure Water 19L", 4: "Aqua 0.5L x12"}
UNIT_PRICE = 15000.0
# `serialize_order_brief` — the FOUR keys the visit's `order` block and the 409
# conflict details carry. The 201 reply carries three more; see `_placed_order`.
EXISTING_ORDER = {"id": 1229, "order_number": "SA_000413_26", "status": "pending",
                  "total_amount": 300000.0}
# The day the BACKEND wrote, deliberately not the day any test taps: the
# agent asks for a day and the backend answers with one (controller ruling
# 35), and only a different date can prove which of the two is printed.
RESOLVED_DATE = "2026-09-10"
RESOLVED_WINDOW = ("09:00", "12:00")


class _FrozenDate(date):
    """`date.today()` pinned to a Tuesday, so "Tomorrow" and "in a week" are assertable.

    Subclassing rather than freezing the clock globally (the technique at
    tests/unit/test_dispatch_service.py:462-470) keeps the parsing in
    `receive_delivery_date` on the real `datetime.strptime` — the half that
    has to reject 01.01.2020 — while only the "what day is it" half is fixed.
    """

    @classmethod
    def today(cls):
        return cls(2026, 9, 8)


def _freeze_today(monkeypatch):
    monkeypatch.setattr(visit_module, "date", _FrozenDate)


def _suggestions():
    """What `POST /stock-check` answered with: eleven-key rows (ruling 4).

    One line the backend measured a rate for, one it did not — the second is
    why "same as last time" exists at all.
    """
    return [
        _stock_row(),
        _stock_row(product_id=4, product_name="Aqua 0.5L x12", on_hand_qty=2, empties_qty=None,
                   is_sold_out=True, is_low=False, suggested_qty=None, rate_per_day=None,
                   rate_source="none", last_order_qty=12),
    ]


def _resumed_checks():
    """`serialize_visit.stock_checks` — the TEN-key rows a resume reads.

    `last_order_qty` rides only on the stock-check reply, so a resumed visit
    legitimately cannot offer "same as last time".
    """
    return [{key: value for key, value in row.items() if key != "last_order_qty"} for row in _suggestions()]


def _estimate_body(request_data):
    """A quote that mirrors the basket, so a wrong basket shows as a wrong total."""
    items = (request_data or {}).get("items") or []
    priced = [{
        "product_id": row["product_id"], "product_name": NAMES[row["product_id"]],
        "quantity": row["quantity"], "unit_price": UNIT_PRICE,
        "total_price": UNIT_PRICE * row["quantity"], "is_contract_price": False,
    } for row in items]
    total = sum(row["total_price"] for row in priced)
    return {"client_id": 41, "currency": "UZS", "items": priced, "subtotal": total,
            "delivery_fee": 0.0, "discount_amount": 0.0, "loyalty_discount": 0.0,
            "tier_discount": 0.0, "total_amount": total}


def _placed_order(window=RESOLVED_WINDOW):
    """`serialize_order_placed` — the brief PLUS the resolved schedule (ruling 35).

    `window=None` is the "anytime" answer the backend gives when the outlet's
    standing hours have already passed for the chosen day
    (`visit_service.py:418-437`): the date still resolves, the window does not.
    """
    start, end = window or (None, None)
    return {**EXISTING_ORDER, "delivery_date": RESOLVED_DATE,
            "delivery_window_start": start, "delivery_window_end": end}


def _order_response(state="pending_confirmation", window=RESOLVED_WINDOW):
    return {"order": _placed_order(window), "confirmation": {"state": state},
            # The VISIT's `order` block is `serialize_order_brief` — four keys,
            # no schedule. Only the top-level `order` carries the resolved one.
            "visit": _visit(current_step="close", checkin_skipped=True, order=dict(EXISTING_ORDER))}


def _close_response(next_due="2026-09-15T04:00:00+00:00"):
    return {"visit": _visit(status="completed", current_step="close",
                            ended_at="2026-09-08T07:40:00+00:00"),
            "outlet": {"id": 7, "name": "Bahor market", "next_visit_due_at": next_due}}


def _abandon_closed_response(next_due="2026-09-21T04:00:00+00:00"):
    """What `POST /visits/{id}/abandon` answers for a visit that ALREADY ordered.

    Ruling 74: the backend CLOSES it rather than abandoning it (`completed` / `order_placed`)
    and answers with the same `{"visit", "outlet"}` card the close route does, so the phone has
    the recomputed due date to print. A different date from `_close_response`'s on purpose: it
    is the only thing that can prove the receipt was rendered from THIS reply.
    """
    return {"visit": _visit(status="completed", current_step="close", outcome="order_placed",
                            ended_at="2026-09-08T07:40:00+00:00", order=dict(EXISTING_ORDER)),
            "outlet": {"id": 7, "name": "Bahor market", "next_visit_due_at": next_due}}


async def _to_order(harness, ops, *, methods=None, suggestions=None):
    """From the outlet card to the order screen, the way an agent gets there."""
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda c: {"visit": _visit(
        current_step="stock", checkin_skipped=True)})
    harness.backend.route("POST", STOCK_CHECK, lambda c: {
        "visit": _visit(current_step="order", checkin_skipped=True),
        "items": _suggestions() if suggestions is None else suggestions})
    harness.backend.route("GET", PAYMENTS, lambda c: {
        "methods": [CASH] if methods is None else methods})
    harness.backend.route("POST", ESTIMATE, lambda c: {"estimate": _estimate_body(c.data)})
    await harness.send(ops.tap("staff_sales_v_skipcheckin"))
    await harness.send(ops.tap("staff_sales_v_stock_3"))
    await harness.send(ops.tap("staff_sales_v_qty_3_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap("staff_sales_v_stock_4"))
    await harness.send(ops.tap("staff_sales_v_qty_4_2"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap("staff_sales_v_stockdone"))


async def test_the_suggested_order_walks_straight_to_the_backend_body(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: _order_response())
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    order_screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in order_screen.text
    assert order_screen.callback_data() == [
        "staff_sales_v_ordersugg", "staff_sales_v_orderedit", "staff_sales_v_orderlast",
        "staff_sales_v_noorder", "staff_sales_v_abandon",
    ]

    await harness.send(ops.tap("staff_sales_v_ordersugg"))

    # One rail on offer, so the agent is never asked to pick it.
    assert len(_calls(harness, "GET", PAYMENTS)) == 1
    assert harness.conversation_state(CONV) == V_DAY
    assert _curated("staff.sales.visit.day_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    assert harness.conversation_state(CONV) == V_NOTES

    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    # The quote carries the RAIL the agent just chose. `create_order` applies
    # a live tier discount per payment method, so an estimate priced without
    # one is a number the store is not going to be charged — the agent reads
    # it out to the shopkeeper on the very next line of this screen.
    assert [c.data for c in _calls(harness, "POST", ESTIMATE)] == [
        {"items": [{"product_id": 3, "quantity": 20}], "payment_method": "cash"},
    ]
    confirm = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.confirm_title") in confirm.text
    # 20 x 15 000, as the BACKEND priced it — the bot multiplies nothing.
    assert "300,000" in confirm.text
    assert "09.09.2026" in confirm.text
    assert _curated("staff.sales.visit.pay.cash") in confirm.text
    assert confirm.callback_data() == [
        "staff_sales_v_orderconfirm", "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_CONFIRM

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert [c.data for c in _calls(harness, "POST", ORDER)] == [{
        "items": [{"product_id": 3, "quantity": 20}],
        "payment_method": "cash",
        "delivery_date": "2026-09-09",
        "delivery_window_start": None,
        "delivery_window_end": None,
        "delivery_notes": None,
    }]
    created = harness.telegram.shown[-2]
    assert "SA_000413_26" in created.text
    assert _curated("staff.sales.visit.order_pending_confirmation") in created.text
    # Controller ruling 35: the receipt prints the schedule the BACKEND wrote,
    # which the agent never typed — the outlet's standing window was filled in
    # and the day resolved to the 10th, not the 9th that was tapped. Echoing
    # the request here is how a shopkeeper is told a window their order has
    # not got.
    assert "10.09.2026" in created.text
    assert "09:00" in created.text and "12:00" in created.text
    assert "09.09.2026" not in created.text
    # An order exists, so how the visit ended is the backend's to stamp: the
    # outcome prompt is skipped entirely.
    close_screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_notes_prompt") in close_screen.text
    assert close_screen.callback_data() == ["staff_sales_v_skip_closenotes", "staff_sales_v_abandon"]
    assert harness.conversation_state(CONV) == V_CLOSE

    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    assert _curated("staff.sales.visit.next_visit_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_next_7"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": None, "no_order_reason": None, "notes": None,
        "next_visit_at": "2026-09-15", "dm_present": None,
    }]
    closed = harness.telegram.last_shown()
    # `visit.closed` carries `{next_due}` (controller ruling 19): the date the
    # agent reads is the BACKEND's recomputed due date, which a predicted
    # stock-out can pull earlier than the seven days just picked.
    # Through production's own renderer, never `str.format`: the interpolation
    # rule belongs to `shared/i18n_rendering.py::render_translation`, which
    # degrades broken copy to the humanised key instead of raising. A local
    # `.format` would keep passing against a value production can no longer
    # render.
    assert _rendered("staff.sales.visit.closed", next_due="15.09.2026") in closed.text
    assert "15.09.2026" in closed.text
    assert _curated("staff.menu.my_outlets") in " ".join(closed.button_labels())
    assert harness.conversation_state(CONV) is None


async def test_changing_a_line_sends_the_changed_basket(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops, methods=[CASH, BUSINESS])
    harness.backend.route("POST", ORDER, lambda c: _order_response(state="confirmed"))

    await harness.send(ops.tap("staff_sales_v_orderedit"))

    edit = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_edit_title") in edit.text
    assert edit.callback_data() == [
        "staff_sales_v_oqty_3", "staff_sales_v_oqty_4", "staff_sales_v_orderdone",
        "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    # Pre-filled from the published `suggested_qty` as-is: None and 0 both open
    # the row on 0. Every fallback and every refusal behind that number is the
    # backend's (`ReplenishmentService.suggested_qty`), so the bot re-derives
    # nothing from `last_order_qty` here.
    assert "Pure Water 19L × 20" in edit.text and "Aqua 0.5L x12 × 0" in edit.text
    # ...and on the BUTTONS, which read `product_name` (controller ruling
    # 32). The text is built here; the labels are built by the keyboard,
    # so only this assertion catches a renamed key.
    assert "Pure Water 19L × 20" in " ".join(edit.button_labels())
    assert harness.conversation_state(CONV) == V_ORDER_EDIT

    await harness.send(ops.tap("staff_sales_v_oqty_4"))
    await harness.send(ops.tap("staff_sales_v_oqtyset_4_6"))
    assert "Aqua 0.5L x12 × 6" in harness.telegram.last_shown().text

    # The registered patterns are `\d+`-wide, so the SUFFIX is the guard: a
    # product this visit never counted and a quantity that is not on the grid
    # are answered and dropped.
    harness.backend.calls.clear()
    await harness.send(ops.tap("staff_sales_v_oqtyset_999_6"))
    await harness.send(ops.tap("staff_sales_v_oqtyset_4_7"))
    assert harness.backend.calls == []
    assert harness.conversation_state(CONV) == V_ORDER_EDIT

    await harness.send(ops.tap("staff_sales_v_orderdone"))

    payment = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.payment_prompt") in payment.text
    assert payment.callback_data() == ["staff_sales_v_pay_cash",
                                       "staff_sales_v_pay_business_account",
                                       "staff_sales_v_abandon"]
    # The LABELS come from the seeded family, never from the backend's
    # `name`: `get_client_payment_methods` hard-codes "Cash on Delivery" /
    # "Business account" in English, and the confirm card one screen later
    # already renders `staff.sales.visit.pay.<method>` -- one flow must not
    # say two different words for the same rail.
    rails = " ".join(payment.button_labels())
    assert _curated("staff.sales.visit.pay.cash") in rails
    assert _curated("staff.sales.visit.pay.business_account") in rails
    # Only the cash rail can prove it from an English journey: the seeded
    # `pay.cash` is "Cash" while the backend's name is "Cash on Delivery",
    # whereas the two agree word for word on the business account. The uz/ru
    # halves of the same rule are pinned in
    # tests/unit/test_sales_visit_bot_plumbing.py.
    assert CASH["name"] not in rails
    assert harness.conversation_state(CONV) == V_PAYMENT

    # `choose_payment` re-validates the next tap against this list, not
    # against the `\w+` pattern (visit.py:1242), so what `_after_lines` WROTE
    # is the guard: the backend's rails, flattened to method strings, in the
    # backend's own order.
    flow = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][FLOW_KEY]
    assert flow["payment_methods"] == ["cash", "business_account"]

    # A rail the backend did not offer is not one this outlet may use.
    await harness.send(ops.tap("staff_sales_v_pay_click"))
    assert harness.conversation_state(CONV) == V_PAYMENT

    await harness.send(ops.tap("staff_sales_v_pay_business_account"))
    await harness.send(ops.tap("staff_sales_v_day_today"))
    await harness.send(ops.text("Ring the side door"))
    assert harness.conversation_state(CONV) == V_CONFIRM

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert [c.data for c in _calls(harness, "POST", ORDER)] == [{
        "items": [{"product_id": 3, "quantity": 20}, {"product_id": 4, "quantity": 6}],
        "payment_method": "business_account",
        "delivery_date": "2026-09-08",
        "delivery_window_start": None,
        "delivery_window_end": None,
        "delivery_notes": "Ring the side door",
    }]
    assert _curated("staff.sales.visit.order_confirmed") in harness.telegram.shown[-2].text


async def test_the_basket_opens_on_a_suggested_zero_not_on_the_last_order(monkeypatch):
    """`suggested_qty == 0` is the backend's ANSWER, not the absence of one.

    `ReplenishmentService.suggested_qty` applies the "else what they took
    last time" fallback itself and returns an explicit 0 for a shelf that is
    already covered. `or` cannot tell that 0 from None, so the edit screen
    opened on last month's 12 crates for a store the rule says needs nothing
    — one tap from an order nobody meant to place.

    The second row is the other half of the same rule: a null `suggested_qty`
    opens the row on 0, exactly as an explicit 0 does. The bot renders what is
    published and applies no fallback of its own — that rule has one owner, and
    it is the backend, which also publishes None where no order can be placed at
    all (a purchase minimum above the line ceiling) while `last_order_qty` stays
    non-null. Reading `last_order_qty` here would overrule that refusal.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    covered = [
        _stock_row(suggested_qty=0, last_order_qty=12),
        _stock_row(product_id=4, product_name="Aqua 0.5L x12", on_hand_qty=2,
                   suggested_qty=None, rate_per_day=None, rate_source="none",
                   last_order_qty=None),
    ]
    await _to_order(harness, ops, suggestions=covered)

    await harness.send(ops.tap("staff_sales_v_orderedit"))

    edit = harness.telegram.last_shown()
    assert "Pure Water 19L × 0" in edit.text
    assert "Pure Water 19L × 12" not in edit.text
    assert "Aqua 0.5L x12 × 0" in edit.text
    assert "Pure Water 19L × 0" in " ".join(edit.button_labels())


async def test_every_screen_after_the_basket_can_still_abandon_and_step_back(monkeypatch):
    """The visit is SERVER-side state, so every screen inside it must end it.

    Five states drew no Abandon at all — the basket, the delivery day, the
    note prompt, the confirm card and the close — and the basket had no route
    back to the order options, so an agent who opened it could no longer reach
    *No order*, the path that records WHY a visit produced nothing. The
    handlers were registered in every one of those states already; only the
    buttons were missing.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ABANDON, lambda c: {"visit": _visit(status="abandoned")})

    await harness.send(ops.tap("staff_sales_v_orderedit"))
    basket = harness.telegram.last_shown()
    assert basket.callback_data() == [
        "staff_sales_v_oqty_3", "staff_sales_v_oqty_4", "staff_sales_v_orderdone",
        "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]

    # One line's grid is a screen of its own, and it could not end the visit.
    await harness.send(ops.tap("staff_sales_v_oqty_3"))
    assert "staff_sales_v_abandon" in harness.telegram.last_shown().callback_data()

    await harness.send(ops.tap("staff_sales_v_orderback"))
    options = harness.telegram.last_shown()
    assert "staff_sales_v_noorder" in options.callback_data()
    assert harness.conversation_state(CONV) == V_ORDER

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    day = harness.telegram.last_shown()
    assert day.callback_data() == [
        "staff_sales_v_day_tomorrow", "staff_sales_v_day_today", "staff_sales_v_day_pick",
        "staff_sales_v_dayback", "staff_sales_v_abandon",
    ]

    # The typed-date prompt and its refusal keep the same way out: both used
    # to render with no keyboard at all.
    await harness.send(ops.tap("staff_sales_v_day_pick"))
    assert harness.telegram.last_shown().callback_data() == day.callback_data()
    await harness.send(ops.text("01.01.2020"))
    invalid = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.day_invalid") in invalid.text
    assert invalid.callback_data() == day.callback_data()

    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    notes = harness.telegram.last_shown()
    assert notes.callback_data() == ["staff_sales_v_skip_notes", "staff_sales_v_abandon"]

    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    confirm = harness.telegram.last_shown()
    assert confirm.callback_data() == [
        "staff_sales_v_orderconfirm", "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]

    await harness.send(ops.tap("staff_sales_v_abandon"))
    assert [c.data for c in _calls(harness, "POST", ABANDON)] == [{}]
    assert harness.conversation_state(CONV) is None


async def test_back_from_the_day_screen_reopens_the_rail_choice(monkeypatch):
    """Back means "the last question I was ASKED", and with two rails that is the rail.

    The rails are the backend's answer, kept on the draft by `_after_lines`;
    re-asking for them here would be a second GET whose only possible effect
    is for the two screens to disagree.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops, methods=[CASH, BUSINESS])
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_pay_business_account"))
    assert harness.conversation_state(CONV) == V_DAY
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_v_dayback"))

    rails = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.payment_prompt") in rails.text
    assert rails.callback_data() == [
        "staff_sales_v_pay_cash", "staff_sales_v_pay_business_account", "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_PAYMENT
    assert _calls(harness, "GET", PAYMENTS) == []


async def test_back_from_the_day_screen_is_the_order_options_when_there_is_one_rail(monkeypatch):
    """A single rail was never a question (`_after_lines` skips the screen),
    so there is nothing to go back TO but the options — where the basket,
    *No order* and Abandon all still live."""
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    assert harness.conversation_state(CONV) == V_DAY

    await harness.send(ops.tap("staff_sales_v_dayback"))

    options = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in options.text
    assert "staff_sales_v_noorder" in options.callback_data()
    assert harness.conversation_state(CONV) == V_ORDER


async def test_same_as_last_time_repeats_what_the_outlet_took(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER,
                          lambda c: _order_response(state="auto_confirmed", window=None))

    await harness.send(ops.tap("staff_sales_v_orderlast"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert [c.data for c in _calls(harness, "POST", ORDER)][0]["items"] == [
        {"product_id": 3, "quantity": 15}, {"product_id": 4, "quantity": 12},
    ]
    receipt = harness.telegram.shown[-2]
    assert _curated("staff.sales.visit.order_auto_confirmed") in receipt.text
    # "Anytime": the date resolved, the window did not. Printing an invented
    # one — or the outlet's hours the backend just refused — is the whole
    # reason the reply carries both fields.
    assert "10.09.2026" in receipt.text
    assert "🕘" not in receipt.text


async def test_the_receipt_prints_the_total_the_store_was_actually_charged(monkeypatch):
    """The quote and the charge are two numbers, and only the second is a bill.

    `serialize_order_placed.total_amount` is what `create_order` WROTE — a
    live tier discount, a contract price, a floor the quote did not know
    about can all move it — and it had no consumer at all: the only money on
    this screen was the estimate, one message above, which the agent has
    already read out to the shopkeeper. The window line is the same rule for
    words: it was a bare pair of times with no label in any language.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    charged = {**_placed_order(), "total_amount": 285000.0}
    harness.backend.route("POST", ORDER, lambda c: {
        "order": charged, "confirmation": {"state": "confirmed"},
        "visit": _visit(current_step="close", checkin_skipped=True, order=dict(EXISTING_ORDER))})
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    assert "300,000" in harness.telegram.last_shown().text  # the QUOTE

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    receipt = harness.telegram.shown[-2]
    assert "SA_000413_26" in receipt.text
    assert "285,000" in receipt.text
    assert "300,000" not in receipt.text
    assert _rendered("staff.sales.visit.window_line", start="09:00", end="12:00") in receipt.text
    assert "10.09.2026" in receipt.text


async def test_no_order_carries_its_reason_into_the_close(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", CLOSE, lambda c: _close_response(next_due=None))

    await harness.send(ops.tap("staff_sales_v_noorder"))

    reasons = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.no_order_reason_prompt") in reasons.text
    assert "staff_sales_v_noreason_price" in reasons.callback_data()
    assert harness.conversation_state(CONV) == V_ORDER

    await harness.send(ops.tap("staff_sales_v_noreason_price"))

    # The reason IS the outcome; asking "how did the visit end?" again would
    # be asking the same question twice.
    assert _curated("staff.sales.visit.close_notes_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CLOSE

    await harness.send(ops.text("Owner wants a discount"))
    assert _curated("staff.sales.visit.next_visit_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_next_none"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": "no_order", "no_order_reason": "price", "notes": "Owner wants a discount",
        "next_visit_at": None, "dm_present": None,
    }]
    assert _calls(harness, "POST", ORDER) == []
    assert harness.conversation_state(CONV) is None


async def test_a_refused_order_keeps_the_confirm_screen_and_closes_nothing(monkeypatch):
    """A 400 at the last step must not look like a placed order OR a closed visit.

    The visit stays open, the basket stays exactly as it was, and the two
    buttons that can still move — send again, or go back and change it — are
    still on the screen.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "This outlet cannot take orders yet", 400, error_code="SALES_OUTLET_NOT_ACTIVE"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.outlet_not_active") in alert for alert in _alerts(harness))
    confirm = harness.telegram.last_shown()
    assert confirm.callback_data() == [
        "staff_sales_v_orderconfirm", "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    assert "300,000" in confirm.text
    assert harness.conversation_state(CONV) == V_CONFIRM
    assert _calls(harness, "POST", CLOSE) == []

    await harness.send(ops.tap("staff_sales_v_orderback"))
    assert harness.conversation_state(CONV) == V_ORDER


async def test_a_refused_order_does_not_redraw_the_confirm_card(monkeypatch):
    """The screen is already correct, so re-sending it is a Telegram error.

    `_show_confirm` renders `_confirm_text(flow, flow['estimate'], ...)` plus
    the same keyboard — and neither the basket nor the cached quote changed,
    so the redraw is byte-identical and Telegram answers "message is not
    modified". The refusal is already on screen as an alert; the redraw only
    turns a handled 400 into a logged 400 of the bot's own making.
    `stock_retry` and `_stay` draw the same line for the same reason.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "This outlet cannot take orders yet", 400, error_code="SALES_OUTLET_NOT_ACTIVE"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    drawn = len([c for c in harness.telegram.of("editMessageText", "sendMessage")
                 if _curated("staff.sales.visit.confirm_title") in c.text])
    assert drawn == 1, "fixture: the confirm card is on screen exactly once"
    # Telegram's real answer to an identical edit.
    harness.telegram.fail("editMessageText", "Message is not modified", 400)

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.outlet_not_active") in alert for alert in _alerts(harness))
    assert len([c for c in harness.telegram.of("editMessageText", "sendMessage")
                if _curated("staff.sales.visit.confirm_title") in c.text]) == drawn
    # The card that IS on screen still carries both moves, and the state
    # still matches it.
    confirm = harness.telegram.last_shown()
    assert confirm.callback_data() == [
        "staff_sales_v_orderconfirm", "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_CONFIRM


async def test_an_order_that_already_exists_moves_on_to_the_close(monkeypatch):
    """409 SALES_VISIT_ORDER_EXISTS means the order IS placed, by an earlier tap.

    Parking the agent on the confirm screen would invite them to send it a
    third time; the visit's remaining work is the close, so that is where
    they land.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "This visit already has an order", 409, error_code="SALES_VISIT_ORDER_EXISTS"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.order_exists") in alert for alert in _alerts(harness))
    close_screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_notes_prompt") in close_screen.text
    assert harness.conversation_state(CONV) == V_CLOSE


async def test_a_409_that_names_the_order_reports_its_number(monkeypatch):
    """The one refusal the bot can report by order number instead of by sentence.

    `_make_request` keeps the error body on `data` for 409s only, so when the
    backend names the order that already exists the agent is told WHICH one —
    the number they can read back to the office — rather than "conflict".
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "This visit already has an order", 409, error_code="SALES_VISIT_ORDER_EXISTS",
        data={"error_code": "SALES_VISIT_ORDER_EXISTS",
              "details": {"order": dict(EXISTING_ORDER)}}))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    receipt = harness.telegram.shown[-2]
    assert "SA_000413_26" in receipt.text
    # No state line: nothing was created now, so how that order was confirmed
    # is not something this reply knows.
    assert _curated("staff.sales.visit.order_pending_confirmation") not in receipt.text
    assert _curated("staff.sales.visit.close_notes_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CLOSE


def test_the_harness_refuses_an_error_body_production_would_drop():
    """`data=` on anything but a 409 is a body production would never hand over.

    `_make_request` attaches the whole response body to `APIResponse.data` on
    a 409 ONLY (staff_bot/api_client.py). On every other status the body reaches
    the handler through `_failure_context` alone -- `details`, the backend's
    `message` and its error type -- which tests drive with
    `staff_backend_failure(details=...)`. The harness used to drop `data=`
    silently, which was the problem: a test written against
    `data={"details": {"max_quantity": 500}}` on a 400 stayed green while the
    handler under test received none of it, and the copy it renders was never
    really exercised.

    Refusing at construction is loud at the line that is wrong.
    """
    kept = staff_backend_failure("This visit already has an order", 409,
                                 error_code="SALES_VISIT_ORDER_EXISTS",
                                 data={"details": {"order": dict(EXISTING_ORDER)}})
    assert kept.data == {"details": {"order": dict(EXISTING_ORDER)}}

    with pytest.raises(ValueError, match="409"):
        staff_backend_failure("That quantity is too large", 400,
                              error_code="SALES_ORDER_QTY_INVALID",
                              data={"details": {"max_quantity": 500}})

    # A refusal with no body is unaffected on every status, including the
    # transport shapes (`status_code=None`) ruling 30's branches are driven with.
    assert staff_backend_failure("upstream is down").data is None
    assert staff_backend_failure("connect failed", None).data is None


async def test_a_picked_date_refuses_the_past_and_takes_a_future_day(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: _order_response())

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_pick"))
    assert _curated("staff.sales.visit.day_pick_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.text("01.01.2020"))

    assert _curated("staff.sales.visit.day_invalid") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_DAY
    assert _calls(harness, "POST", ESTIMATE) == []

    await harness.send(ops.text("2026-10-01"))
    assert harness.conversation_state(CONV) == V_NOTES

    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    assert "01.10.2026" in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))
    assert [c.data for c in _calls(harness, "POST", ORDER)][0]["delivery_date"] == "2026-10-01"


async def test_a_resumed_visit_lands_on_the_order_screen(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="order", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })

    await harness.send(ops.tap("staff_sales_visit_resume"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in screen.text
    assert "Pure Water 19L" in screen.text and "20" in screen.text
    # No `last_order_qty` on a resumed visit's rows, so no "same as last time"
    # button offering a quantity nobody can see.
    assert screen.callback_data() == ["staff_sales_v_ordersugg", "staff_sales_v_orderedit",
                                      "staff_sales_v_noorder", "staff_sales_v_abandon"]
    assert harness.conversation_state(CONV) == V_ORDER


async def test_a_resumed_visit_with_an_order_never_asks_for_the_outcome(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks(),
                        order=dict(EXISTING_ORDER)),
        "outlet": {"id": 7, "name": "Bahor market"},
    })

    await harness.send(ops.tap("staff_sales_visit_resume"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_notes_prompt") in screen.text
    assert screen.callback_data() == ["staff_sales_v_skip_closenotes", "staff_sales_v_abandon"]
    assert harness.conversation_state(CONV) == V_CLOSE


async def test_a_resumed_visit_without_an_order_asks_how_it_ended(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_outcome_prompt") in screen.text
    assert screen.callback_data() == [
        "staff_sales_v_outcome_no_order", "staff_sales_v_outcome_closed",
        "staff_sales_v_outcome_owner_absent", "staff_sales_v_outcome_refused",
        "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_CLOSE

    # `no_order` without a reason is a close the backend refuses, so the bot
    # asks for the reason rather than sending it and reading the 400 back.
    await harness.send(ops.tap("staff_sales_v_outcome_no_order"))
    assert _curated("staff.sales.visit.no_order_reason_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_noreason_cash_issue"))
    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    await harness.send(ops.tap("staff_sales_v_next_3"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": "no_order", "no_order_reason": "cash_issue", "notes": None,
        "next_visit_at": "2026-09-11", "dm_present": None,
    }]
    assert harness.conversation_state(CONV) is None


async def test_an_outlet_with_no_rail_says_so_and_keeps_the_basket(monkeypatch):
    """`GET /payment-methods` answering `[]` is the outlet, not the network.

    An outlet whose account is not activated can be visited and counted but
    not ordered for. Saying so at the rail step is kinder than a 400 read at
    the end of the visit — and the basket stays, because it is still valid for
    the moment someone activates the account (controller ruling 24).

    The copy is its OWN key: `error.outlet_not_active` says "ask an operator
    to activate this outlet", which is the wrong instruction for an ACTIVE
    outlet whose COD is capped or whose contract is unsigned — the commonest
    way this screen is reached.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops, methods=[])

    await harness.send(ops.tap("staff_sales_v_ordersugg"))

    assert any(_curated("staff.sales.visit.no_rails") in alert for alert in _alerts(harness))
    assert not any(_curated("staff.sales.error.outlet_not_active") in alert
                   for alert in _alerts(harness))
    # The screen does not move, and nothing was sent anywhere.
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in screen.text
    assert _calls(harness, "POST", ORDER) == []
    assert _calls(harness, "POST", ESTIMATE) == []
    assert harness.conversation_state(CONV) == V_ORDER
    flow = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][FLOW_KEY]
    assert flow["order_lines"] == {"3": 20}
    assert flow["payment_method"] is None
    # NOT `flow["payment_methods"] == []`: `_seed` initialises that key to `[]`
    # (visit.py:279) and `_after_lines` writes it only when the list is
    # non-empty, so `== []` held whether the handler ran, returned early or
    # raised before reaching the rails. What the handler actually did is on the
    # screen: the rail question was never asked, on any message of this journey.
    assert not any(_curated("staff.sales.visit.payment_prompt") in text
                   for text in harness.telegram.texts())


async def test_a_visit_that_found_nobody_closes_without_a_reason(monkeypatch):
    """`owner_absent` is an outcome that has no "why no order" to give.

    Only `no_order` opens the reason grid; the other three outcomes go
    straight to the note, and the close body carries `no_order_reason: null`
    rather than a reason invented to fill the field.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))
    await harness.send(ops.tap("staff_sales_v_outcome_owner_absent"))

    notes = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_notes_prompt") in notes.text
    assert notes.callback_data() == ["staff_sales_v_skip_closenotes", "staff_sales_v_abandon"]

    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    await harness.send(ops.tap("staff_sales_v_next_14"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": "owner_absent", "no_order_reason": None, "notes": None,
        "next_visit_at": "2026-09-22", "dm_present": None,
    }]
    # The grid was never drawn at all, on any message of the journey.
    assert not any(_curated("staff.sales.visit.no_order_reason_prompt") in text
                   for text in harness.telegram.texts())
    assert harness.conversation_state(CONV) is None


async def test_a_stale_skip_tap_cannot_jump_the_reason_grid(monkeypatch):
    """The Skip button belongs to the NOTE question, not to whatever is on screen.

    Telegram keeps every old keyboard live, so the Skip from a previous
    close screen can arrive while the reason grid is up. Filing it as "no
    note" would move the close to the next-visit question with the reason
    still missing, and `POST /close` then answers
    SALES_VISIT_OUTCOME_REQUIRED to every tap after that — a loop the agent
    cannot leave without abandoning the visit.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))
    await harness.send(ops.tap("staff_sales_v_outcome_no_order"))
    assert _curated("staff.sales.visit.no_order_reason_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))

    still = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.no_order_reason_prompt") in still.text
    assert "staff_sales_v_noreason_price" in still.callback_data()
    assert _curated("staff.sales.visit.next_visit_prompt") not in " ".join(harness.telegram.texts())
    assert harness.conversation_state(CONV) == V_CLOSE
    assert _calls(harness, "POST", CLOSE) == []

    # ...and the real answer still works, one tap later.
    await harness.send(ops.tap("staff_sales_v_noreason_other"))
    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    await harness.send(ops.tap("staff_sales_v_next_30"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": "no_order", "no_order_reason": "other", "notes": None,
        "next_visit_at": "2026-10-08", "dm_present": None,
    }]
    assert harness.conversation_state(CONV) is None


async def test_an_order_post_that_may_have_landed_says_so(monkeypatch):
    """The AMBIGUOUS transport failure, and the only sentence that is not a guess.

    A `POST /visits/<id>/order` that dies in the read/write phase may or may
    not have created an order — and this POST is never auto-retried, because
    `visit_id` is the idempotency key on the SERVER, not on the phone. Telling
    the agent it failed invites a second tap and a second order for the same
    visit (controller ruling 30, spec *Failure modes*), so the bot says what it
    knows, hands over the Resume that re-reads the server, and gets out of
    the way.

    `TRANSPORT_AMBIGUOUS_ERROR_CODE` is what the CLIENT stamps for that phase
    (`_make_request`, `AMBIGUOUS_PHASE_ERRORS`); the connect phase is a
    different answer and gets the test below.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "Request failed after retries", status_code=None,
        error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_maybe_landed") in screen.text
    assert screen.callback_data() == ["staff_sales_visit_resume"]
    # Exactly one attempt, and no close: the visit is still open on the
    # server, which is what the Resume button will find.
    assert len(_calls(harness, "POST", ORDER)) == 1
    assert _calls(harness, "POST", CLOSE) == []
    assert harness.conversation_state(CONV) is None
    assert _curated("staff.sales.visit.order_created") not in screen.text


async def test_an_order_post_that_never_left_the_phone_keeps_the_confirm_screen(monkeypatch):
    """A connect-phase failure PROVABLY never reached the backend.

    `_make_request` splits the two phases and says which one happened: it
    stamps `TRANSPORT_AMBIGUOUS_ERROR_CODE` on the give-up response for
    read/write failures and leaves `error_code` None for a connect-phase
    exhaustion (no connection, or none out of the pool). Treating both as "the
    order may have been placed" ends the conversation and tells an agent to go
    and check for an order that cannot exist — and a warning that cries wolf is
    one they stop reading, which is the warning above.

    So the connect phase is an ordinary refusal: say the service is
    unavailable, and leave the agent on the confirm screen with Send again
    still on it. (`staff_bot/handlers/delivery/bottle_collection.py:201-222`
    draws the same line for the bottle writes.)
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "Request failed after retries", status_code=None))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.error.api.service_unavailable") in alert for alert in _alerts(harness))
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_maybe_landed") not in screen.text
    assert screen.callback_data() == [
        "staff_sales_v_orderconfirm", "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    assert "300,000" in screen.text
    assert harness.conversation_state(CONV) == V_CONFIRM
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]

    # ...and sending again is the right advice here: nothing was created, so
    # the retry creates exactly one order.
    harness.backend.route("POST", ORDER, lambda c: _order_response())
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert len(_calls(harness, "POST", ORDER)) == 2
    assert "SA_000413_26" in harness.telegram.shown[-2].text
    assert harness.conversation_state(CONV) == V_CLOSE


async def test_abandoning_from_the_order_screen_posts_and_ends(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ABANDON, lambda c: {"visit": _visit(status="abandoned")})

    await harness.send(ops.tap("staff_sales_v_abandon"))

    assert [c.data for c in _calls(harness, "POST", ABANDON)] == [{}]
    assert _curated("staff.sales.visit.abandoned") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) is None


async def test_abandoning_after_the_order_reads_as_a_CLOSE_not_as_abandoned(monkeypatch):
    """Ruling 74: the post-order screens draw Abandon too, and the server closes those visits.

    The agent has to leave the shop knowing what really happened to the call they just made: the
    order stands, the visit is closed, and the shop's next visit is on the recomputed date. The
    bot reads the SERVER's `visit.status` to tell the two endings apart -- it cannot decide from
    the flow which screen the tap came from, because the same button posts from all of them.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: _order_response(state="confirmed"))
    harness.backend.route("POST", ABANDON, lambda c: _abandon_closed_response())

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))
    close_screen = harness.telegram.last_shown()
    assert close_screen.callback_data() == ["staff_sales_v_skip_closenotes", "staff_sales_v_abandon"]

    await harness.send(ops.tap("staff_sales_v_abandon"))

    assert [c.data for c in _calls(harness, "POST", ABANDON)] == [{}]
    # Nothing was posted to `close`: the ONE call that ended the visit is the abandon tap, and
    # the copy below is the server's answer to it rather than a second round trip.
    assert _calls(harness, "POST", CLOSE) == []
    ended = harness.telegram.last_shown()
    assert _rendered("staff.sales.visit.closed", next_due="21.09.2026") in ended.text
    assert "21.09.2026" in ended.text
    assert _curated("staff.sales.visit.abandoned") not in ended.text
    assert _curated("staff.menu.my_outlets") in " ".join(ended.button_labels())
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_menu_tap_mid_order_leaves_the_visit_open(monkeypatch):
    """Walking away is not abandoning.

    The half-built basket is phone-side and goes; the VISIT is server-side
    and stays, which is what makes the card's "Resume visit" mean anything.
    Only `staff_sales_v_abandon` ends one, and it says so on the button.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_order(harness, ops)

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))

    assert harness.conversation_state(CONV) is None
    assert _calls(harness, "POST", ABANDON) == []
    assert _calls(harness, "POST", CLOSE) == []
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text


# ---------------------------------------------------------------------------
# The three refusals the FIRST half owns. They live here because this is the
# file that has `staff_backend_failure` driving whole journeys: each asserts
# the screen the agent is left looking at, not that a call was made.
# ---------------------------------------------------------------------------
async def test_a_refused_checkin_hands_the_location_button_back(monkeypatch):
    """The pin collapsed the one-shot keyboard; a refusal has to re-send it.

    Without the re-send the agent is standing at the door with no button to
    retry with and no way back to the shelf — the visit is open on the server
    and unreachable from the phone.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda c: staff_backend_failure("upstream is down"))

    await harness.send(ops.location(*DOOR, horizontal_accuracy=ACCURACY))

    # A pin is a MESSAGE, not a tap: there is no callback to answer, so the
    # refusal arrives as a reply rather than as an alert.
    assert _alerts(harness) == []
    assert any(_curated("staff.error.api.service_unavailable") in text for text in harness.telegram.texts())
    prompt = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.checkin_prompt") in prompt.text
    assert _curated("staff.sales.visit.checkin_button") in " ".join(prompt.button_labels())
    assert harness.conversation_state(CONV) == V_CHECKIN


async def test_a_refused_stock_check_leaves_the_counted_shelf_on_screen(monkeypatch):
    """A 400 on the counts must not clear them: the shelf was counted once."""
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    # `VisitService._qty`'s refusal of the 4 counted below, on a deployment
    # whose `SALES_STOCK_QTY_MAX` is 3. It takes the handler's plain "stay on
    # the shelf" branch (SALES_STOCK_PRODUCT_INVALID re-fetches instead).
    harness.backend.route("POST", STOCK_CHECK, lambda c: staff_backend_failure(
        "on_hand_qty must be between 0 and 3", 400, "SALES_STOCK_QTY_INVALID",
        details={"field": "on_hand_qty", "value": 4, "max": 3}, error_type="VALIDATION_ERROR",
    ))

    await harness.send(ops.tap("staff_sales_v_stock_3"))
    await harness.send(ops.tap("staff_sales_v_qty_3_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap("staff_sales_v_stockdone"))

    # Since Task 2 the client keeps `details` on a 400, and Task 11 gave this
    # code a detail-copy spec, so the `max` published above now renders the
    # specific sentence rather than the generic fallback key.
    expected = _curated("staff.sales.error.stock_qty_detail").format(maximum=3)
    assert any(expected in alert for alert in _alerts(harness))
    shelf = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.stock_title") in shelf.text
    assert f"Pure Water 19L — {_curated('staff.sales.visit.stock_row')}: 4" in shelf.text
    assert "staff_sales_v_stockdone" in shelf.callback_data()
    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.order_title") not in shelf.text


async def test_a_refused_abandon_leaves_the_agent_on_the_order_screen(monkeypatch):
    """Abandon returns None on failure — PTB reads that as "state unchanged".

    The draft survives too: an agent whose Abandon was refused still has the
    basket they built, and the visit is still open on the server.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_order(harness, ops)
    # `VisitService.get_owned`'s refusal, exactly as the abandon route raises it.
    harness.backend.route("POST", ABANDON, lambda c: staff_backend_failure(
        "Visit belongs to another agent", 403, "SALES_VISIT_NOT_OWNED", error_type="FORBIDDEN",
    ))

    await harness.send(ops.tap("staff_sales_v_abandon"))

    assert any(_curated("staff.error.api.visit_not_owned") in alert for alert in _alerts(harness))
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in screen.text
    assert screen.callback_data() == [
        "staff_sales_v_ordersugg", "staff_sales_v_orderedit", "staff_sales_v_orderlast",
        "staff_sales_v_noorder", "staff_sales_v_abandon",
    ]
    assert _curated("staff.sales.visit.abandoned") not in screen.text
    assert harness.conversation_state(CONV) == V_ORDER
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_stale_next_visit_tap_cannot_close_an_ordered_visit_early(monkeypatch):
    """The next-visit grid belongs to the LAST close question, not to any screen.

    Telegram keeps every old keyboard live, so a `staff_sales_v_next_*` tap
    from a previous visit's close screen can arrive while the note prompt (or
    the outcome/reason grid) is still up. Taking it would POST the close right
    then: an ordered visit filed with no note, and `next_visit_at` set from a
    button the agent never meant for THIS shop — the backend then folds that
    date into `next_visit_due_at` and the outlet is scheduled wrong.

    Its two siblings on this screen (`receive_close_notes`, `skip_close_notes`)
    already refuse a tap that does not belong to the question on screen; this
    is the same guard for the grid that ends the visit.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks(),
                        order=dict(EXISTING_ORDER)),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))
    assert _curated("staff.sales.visit.close_notes_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_next_7"))

    assert _calls(harness, "POST", CLOSE) == []
    assert _curated("staff.sales.visit.close_notes_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CLOSE

    # ...and the real answer still works, one tap later.
    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    await harness.send(ops.tap("staff_sales_v_next_7"))

    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": None, "no_order_reason": None, "notes": None,
        "next_visit_at": "2026-09-15", "dm_present": None,
    }]
    assert harness.conversation_state(CONV) is None


async def test_a_stale_next_visit_tap_cannot_jump_the_outcome_grid(monkeypatch):
    """The same guard on the FIRST close question, where the damage is worse.

    With the outcome still unanswered the close posts `outcome: null` on a
    visit that has no order, and the backend answers
    SALES_VISIT_OUTCOME_REQUIRED — a refusal the agent can do nothing about
    from a screen that has already moved on.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))
    assert _curated("staff.sales.visit.close_outcome_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_next_3"))

    assert _calls(harness, "POST", CLOSE) == []
    assert _curated("staff.sales.visit.close_outcome_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CLOSE


async def test_a_sub_minimum_line_sends_the_agent_back_to_the_basket(monkeypatch):
    """`min_order_quantity` is published to every surface and was read by none.

    The floor lives on the backend (`create_order`, and now `order_estimate` /
    `place_order` as the coded `SALES_ORDER_MIN_QTY`), and the row carrying it
    has ridden through this flow since Task 11 without a screen ever printing
    it. The refusal is a 400, and the staff client keeps an error body on
    `data` for 409s ONLY — so the bot has the CODE and nothing else: no product
    name, no numbers. It therefore says the generic sentence and puts the agent
    back on the one screen where a quantity can be changed, with the floor
    printed beside each line.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE, lambda c: staff_backend_failure(
        "Pure Water 19L: minimum order quantity is 2 (you ordered 1)", 400,
        error_code="SALES_ORDER_MIN_QTY"))

    await harness.send(ops.tap("staff_sales_v_orderedit"))
    await harness.send(ops.tap("staff_sales_v_oqty_3"))

    # The floor is printed where the quantity is chosen — product 3's
    # `min_order_quantity` is 2, product 4's is 1 and prints nothing.
    grid = harness.telegram.last_shown()
    assert f"{_curated('staff.sales.visit.order_min')} 2" in grid.text

    await harness.send(ops.tap("staff_sales_v_oqtyset_3_1"))
    await harness.send(ops.tap("staff_sales_v_orderdone"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    assert any(_curated("staff.sales.error.order_min_qty") in alert for alert in _alerts(harness))
    basket = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_edit_title") in basket.text
    assert basket.callback_data() == [
        "staff_sales_v_oqty_3", "staff_sales_v_oqty_4", "staff_sales_v_orderdone",
        "staff_sales_v_orderback", "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_ORDER_EDIT
    # The confirm screen was never reached, so nothing could be sent from it.
    assert _curated("staff.sales.visit.confirm_title") not in " ".join(harness.telegram.texts())
    assert _calls(harness, "POST", ORDER) == []

    await harness.send(ops.tap("staff_sales_v_oqty_4"))
    assert f"{_curated('staff.sales.visit.order_min')}" not in harness.telegram.last_shown().text


async def test_the_order_post_refusing_the_minimum_also_lands_on_the_basket(monkeypatch):
    """The same code from the WRITE, which the quote can miss.

    The estimate and the order are two calls, and only the second one is the
    charge: an admin can raise `min_order_quantity` between them. Leaving the
    agent on the confirm screen would offer Send again, on a basket the
    backend has already said it will not take.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "Pure Water 19L: minimum order quantity is 40 (you ordered 20)", 400,
        error_code="SALES_ORDER_MIN_QTY"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.order_min_qty") in alert for alert in _alerts(harness))
    basket = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_edit_title") in basket.text
    assert harness.conversation_state(CONV) == V_ORDER_EDIT
    assert len(_calls(harness, "POST", ORDER)) == 1


async def test_a_minimum_quantity_refusal_names_the_product_and_the_floor(monkeypatch):
    """The backend always published product, floor and quantity in `details`;
    the client dropped them on every 400 until 2026-09-24."""
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "Pure Water 19L: minimum order quantity is 40 (you ordered 20)", 400,
        error_code="SALES_ORDER_MIN_QTY",
        details={"product_name": "Pure Water 19L", "min_order_quantity": 40, "quantity": 20},
    ))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    expected = _curated("staff.sales.error.order_min_qty_detail").format(
        product="Pure Water 19L", minimum=40, quantity=20
    )
    assert any(expected in shown for shown in _alerts(harness) + [c.text for c in harness.telegram.shown])
    assert harness.conversation_state(CONV) == V_ORDER_EDIT


async def test_a_refused_delivery_date_re_asks_the_day(monkeypatch):
    """The backend owns the calendar, and the day is not on the confirm card.

    `parse_and_validate_schedule` refuses dates the bot's own "not in the
    past" rule lets through — too far ahead, or a today whose delivery window
    has already closed. Coded (`SALES_DELIVERY_DATE_INVALID`), the refusal can
    land on the ONE screen where a day can be changed; before it, a code-less
    400 left the agent on the confirm card whose only moves are Send again
    (the same date) and Back (the order options, three screens away).
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "delivery_date cannot be booked for this outlet", 400,
        error_code="SALES_DELIVERY_DATE_INVALID"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_today"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.visit.day_invalid") in alert for alert in _alerts(harness))
    day = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.day_prompt") in day.text
    assert "staff_sales_v_day_tomorrow" in day.callback_data()
    assert harness.conversation_state(CONV) == V_DAY
    assert len(_calls(harness, "POST", ORDER)) == 1

    # The basket is untouched, so answering the question sends it — and the
    # answer goes straight back to the confirm card, with no second note prompt
    # in between (M04), so there is no Skip to tap here any more.
    harness.backend.route("POST", ORDER, lambda c: _order_response())
    harness.backend.route("POST", CLOSE, lambda c: _close_response())
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    assert harness.conversation_state(CONV) == V_CONFIRM
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    sent = [c.data for c in _calls(harness, "POST", ORDER)]
    assert sent[1]["delivery_date"] == "2026-09-09"
    assert sent[1]["items"] == [{"product_id": 3, "quantity": 20}]


async def test_the_refused_day_is_re_asked_alone_and_the_typed_note_survives(monkeypatch):
    """M04: the recovery re-asked the NOTE too, and Skip is what deletes one.

    The branch's own comment says "the basket, the rail and the note are all
    still on the draft, so one answer re-sends". They were on the draft, but the
    answer did not re-send: `choose_day` walks on to the note prompt, whose
    ⏭ Skip writes `delivery_notes = None`. So an agent who had typed "leave it
    with the guard", hit a refused date and answered it was offered a Skip that
    silently deleted the instruction they had already given — the delivery
    instruction, on the order that is about to be sent. The DAY is the only
    field the confirm card cannot change, so the day is the only thing re-asked.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "delivery_date cannot be booked for this outlet", 400,
        error_code="SALES_DELIVERY_DATE_INVALID"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_today"))
    await harness.send(ops.text("Leave it with the guard at the back gate"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert harness.conversation_state(CONV) == V_DAY

    harness.backend.route("POST", ORDER, lambda c: _order_response())
    harness.backend.route("POST", CLOSE, lambda c: _close_response())
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))

    # Straight back to the card that was refused — no second note prompt, so no
    # Skip to tap and nothing to lose.
    assert harness.conversation_state(CONV) == V_CONFIRM
    assert _curated("staff.sales.visit.notes_prompt") not in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    sent = [c.data for c in _calls(harness, "POST", ORDER)]
    assert len(sent) == 2
    assert sent[1]["delivery_date"] == "2026-09-09"
    assert sent[1]["delivery_notes"] == "Leave it with the guard at the back gate"


async def test_a_quantity_over_the_ceiling_lands_on_the_basket(monkeypatch):
    """`SALES_ORDER_QTY_INVALID` is the minimum's sibling, from the other end.

    Both are 400s, so the client keeps no body and the bot has the CODE and
    nothing else — no product name, no numbers. It therefore says the generic
    sentence and puts the agent on the one screen where a quantity can be
    changed, exactly as the floor does.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE, lambda c: staff_backend_failure(
        "Pure Water 19L: 800 is above the maximum of 500", 400,
        error_code="SALES_ORDER_QTY_INVALID"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    assert any(_curated("staff.sales.error.order_qty") in alert for alert in _alerts(harness))
    basket = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_edit_title") in basket.text
    assert harness.conversation_state(CONV) == V_ORDER_EDIT
    assert _curated("staff.sales.visit.confirm_title") not in " ".join(harness.telegram.texts())
    assert _calls(harness, "POST", ORDER) == []


async def test_the_order_post_refusing_the_ceiling_also_lands_on_the_basket(monkeypatch):
    """The same code from the WRITE, which the quote can miss: the estimate and
    the order are two calls and only the second one bills."""
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "Pure Water 19L: 800 is above the maximum of 500", 400,
        error_code="SALES_ORDER_QTY_INVALID"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.order_qty") in alert for alert in _alerts(harness))
    assert _curated("staff.sales.visit.order_edit_title") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_ORDER_EDIT
    assert len(_calls(harness, "POST", ORDER)) == 1


async def test_a_failed_quote_never_reaches_the_confirm_screen(monkeypatch):
    """No money line means no confirm screen — the two are the same screen.

    The confirm card was built to be sent "anyway": `_confirm_text` reads
    `estimate['total_amount']` and simply omits the 💰 line when there is no
    quote, leaving Place order live under a basket with no price on it. That is
    the one screen where the agent reads a number out to the shopkeeper, and a
    send from it commits the store to a total nobody has seen. A quote the
    backend refused is not a quote to confirm against: the agent goes back to
    the order options with the error, where every move (re-price, edit, no
    order, abandon) is still available.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ESTIMATE,
                          lambda c: staff_backend_failure("upstream is down"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    assert any(_curated("staff.error.api.service_unavailable") in alert for alert in _alerts(harness))
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.order_title") in screen.text
    assert screen.callback_data() == [
        "staff_sales_v_ordersugg", "staff_sales_v_orderedit", "staff_sales_v_orderlast",
        "staff_sales_v_noorder", "staff_sales_v_abandon",
    ]
    assert harness.conversation_state(CONV) == V_ORDER
    # Neither the confirm screen nor a send from it ever happened.
    assert _curated("staff.sales.visit.confirm_title") not in " ".join(harness.telegram.texts())
    assert _calls(harness, "POST", ORDER) == []
    assert harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][FLOW_KEY]["estimate"] is None

    # The basket survived it, so one tap re-prices and the flow continues.
    harness.backend.route("POST", ESTIMATE, lambda c: {"estimate": _estimate_body(c.data)})
    harness.backend.route("POST", ORDER, lambda c: _order_response())
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))

    assert "300,000" in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CONFIRM


async def test_a_visit_closed_under_the_agent_ends_the_flow_instead_of_looping(monkeypatch):
    """`SALES_VISIT_NOT_OPEN` means the row is gone, so the screen is a dead end.

    The stale-visit sweep closes an in_progress visit that has been open too
    long, and a second device or an admin can end one at any moment. Every
    button on the screen the agent is looking at posts to that id, so leaving
    them there is a loop: the same refusal to every tap, with no way out but
    Abandon — which posts to the same dead id.

    The shape is the one the backend really sends (409, top-level
    `error_code`), not an invented one: `VisitService` raises the same
    `ConflictError` from `stock_check`, `place_order` and `close`.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", STOCK_CHECK, lambda c: staff_backend_failure(
        "This visit is not open", 409, error_code="SALES_VISIT_NOT_OPEN"))

    await harness.send(ops.tap("staff_sales_v_stock_3"))
    await harness.send(ops.tap("staff_sales_v_qty_3_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert any(_curated("staff.sales.error.visit_not_open") in alert for alert in _alerts(harness))
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_the_order_post_on_a_closed_visit_also_ends_the_flow(monkeypatch):
    """The same refusal from the write that matters most.

    `place_order` re-checks `visit.status` under its own lock, so the sweep can
    close the visit between the confirm screen and the tap on it. Re-showing
    the confirm screen there offers Send again on a visit that no longer
    exists.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: staff_backend_failure(
        "This visit is not open", 409, error_code="SALES_VISIT_NOT_OPEN"))

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert any(_curated("staff.sales.error.visit_not_open") in alert for alert in _alerts(harness))
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert _calls(harness, "POST", CLOSE) == []
    # The confirm card stays on screen and is now inert, which is the honest
    # picture: nothing was placed and nothing was closed. A later tap on it
    # reaches the conversation's stale-tap entry point, which re-reads the
    # server rather than posting to a dead visit id.
    assert len(_calls(harness, "POST", ORDER)) == 1


async def test_a_stale_send_tap_from_a_screen_the_agent_has_left_never_sends_it_twice(monkeypatch):
    """A tap whose PREFIX belongs to a state this conversation is no longer in.

    `test_junk_taps_are_answered_without_touching_the_backend` (visit
    journey) and the order-edit equivalent above cover a bad SUFFIX on a pattern
    the CURRENT state registers -- the per-screen `_stay` guards. The other
    shape is this one: the order is sent, the agent is on the close screen, and
    the confirm message one scroll up still carries a live ✅ Send. Telegram
    never expires it.

    V_CLOSE registers no `^staff_sales_v_orderconfirm$` handler, so the tap
    reaches the conversation's LAST entry point, `_StaleFlowTap`
    (staff_bot/bot.py:1548). What it does there is a money question: re-read the
    server, or post a second order against the same visit.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)
    harness.backend.route("POST", ORDER, lambda c: _order_response())

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))
    await harness.send(ops.tap("staff_sales_v_skip_notes"))
    await harness.send(ops.tap("staff_sales_v_orderconfirm"))
    assert harness.conversation_state(CONV) == V_CLOSE
    assert len(_calls(harness, "POST", ORDER)) == 1

    # What the SERVER says the visit is now: ordered, waiting to be closed.
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks(),
                        order=dict(EXISTING_ORDER)),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    # Nothing was written -- not a second order, not a close.
    assert [c for c in harness.backend.calls if c.method == "POST"] == []
    assert len(_calls(harness, "GET", CURRENT)) == 1
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.close_notes_prompt") in screen.text
    # The close-note prompt carries the Abandon every screen inside a visit
    # carries (Task 7 step 27 added it to `visit_skip`).
    assert screen.callback_data() == ["staff_sales_v_skip_closenotes", "staff_sales_v_abandon"]
    assert harness.conversation_state(CONV) == V_CLOSE


async def test_the_grids_refuse_a_value_they_never_offered(monkeypatch):
    """Every visit pattern is `\\w+`-wide, so the VALUE is the guard.

    Four grids, four vocabularies, and one of them is not merely junk:
    `order_placed` is a REAL outcome the backend stamps itself when the visit
    carries an order, and it is deliberately absent from `CLOSE_OUTCOMES` — an
    agent must not be able to claim an order that has no row. A tap that
    arrives for it anyway (an older keyboard, a hand-built callback) is
    answered and dropped, not filed.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda c: {
        "visit": _visit(current_step="close", checkin_skipped=True, stock_checks=_resumed_checks()),
        "outlet": {"id": 7, "name": "Bahor market"},
    })
    harness.backend.route("POST", CLOSE, lambda c: _close_response())

    await harness.send(ops.tap("staff_sales_visit_resume"))
    assert harness.conversation_state(CONV) == V_CLOSE
    harness.backend.calls.clear()

    # The outcome the BACKEND stamps, and three values no grid ever drew.
    await harness.send(ops.tap("staff_sales_v_outcome_order_placed"))
    await harness.send(ops.tap("staff_sales_v_noreason_because"))
    await harness.send(ops.tap("staff_sales_v_next_99"))

    assert harness.backend.calls == []
    assert _curated("staff.sales.visit.close_outcome_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_CLOSE

    # ...and the day grid, on the other side of the flow.
    await harness.send(ops.tap("staff_sales_v_outcome_no_order"))
    await harness.send(ops.tap("staff_sales_v_noreason_price"))
    await harness.send(ops.tap("staff_sales_v_skip_closenotes"))
    assert _curated("staff.sales.visit.next_visit_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_next_none"))
    assert [c.data for c in _calls(harness, "POST", CLOSE)] == [{
        "outcome": "no_order", "no_order_reason": "price", "notes": None,
        "next_visit_at": None, "dm_present": None,
    }]


async def test_the_day_grid_refuses_a_choice_it_never_offered(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    _freeze_today(monkeypatch)
    await _to_order(harness, ops)

    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    assert harness.conversation_state(CONV) == V_DAY
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_v_day_yesterday"))

    assert harness.backend.calls == []
    assert harness.conversation_state(CONV) == V_DAY
    assert _curated("staff.sales.visit.day_prompt") in harness.telegram.last_shown().text
