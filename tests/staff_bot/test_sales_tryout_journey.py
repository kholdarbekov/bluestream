"""A sales agent lends a shop some water, end to end through the real PTB Application.

The product catalogue is the CUSTOMER endpoint's own shape -- `is_active` under
`flags`, `is_tryout_eligible` under `inventory` -- because that is where
`serialize_product` puts them. A fixture that flattened them would make the
eligibility filter untestable: read at the top level the keys are absent, and a
defaulting read passes every product through (which is what the driver's older
try-out screen does today).
"""
import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.sales.tryout import T_CONFIRM, T_NOTES, T_PRODUCTS, T_QTY
from staff_bot.handlers.sales.visit import FLOW_KEY as VISIT_FLOW_KEY, V_ORDER
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import (
    OUTLETS,
    _agent,
    _alerts,
    _calls,
    _card,
    _curated,
    _label,
    _rendered,
)
from tests.staff_bot.test_sales_visit_journey import CONV as VISIT_CONV
from tests.staff_bot.test_sales_visit_orders_journey import _to_order
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PRODUCTS = "/api/v1/products/"
TRYOUTS = f"{OUTLETS}/5/tryouts"
CONV = "staff_sales_tryout"


def _product(product_id, name, *, eligible=True, active=True):
    """`serialize_product`, trimmed to the four keys this screen reads."""
    return {
        "id": product_id,
        "name": name,
        "flags": {"is_active": active, "is_featured": False},
        "inventory": {
            "is_tryout_eligible": eligible,
            "is_returnable_bottle": True,
            "min_order_quantity": 1,
        },
    }


CATALOGUE = [
    _product(11, "Pure Water 19L"),
    _product(12, "Pure Water 10L"),
    _product(13, "Ice Tea 1L", eligible=False),
    _product(14, "Retired SKU", active=False),
]


def _tryout(**over):
    """`TryoutService.serialize_tryout`, trimmed to what the receipt reads."""
    base = {
        "id": 77,
        "tryout_number": "TRY_000077_26",
        "status": "scheduled",
        "source": "sales_agent",
        "items": [{"product_id": 11, "product_name": "Pure Water 19L", "quantity": 2}],
    }
    base.update(over)
    return base


async def _open_tryout(harness, ops, *, catalogue=None, stage="prospect"):
    """Open the card and tap 🧪 Try-out — the only way in."""
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(stage=stage)})
    harness.backend.route("GET", PRODUCTS,
                          lambda c: {"items": list(CATALOGUE if catalogue is None else catalogue)})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    await harness.send(ops.tap("staff_sales_tryout_5"))


async def test_happy_path_posts_the_exact_payload_and_renders_the_receipt(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    # Task 5's 201 shape: the try-out AND the outlet card the transition just
    # produced, so the receipt can be the card.
    harness.backend.route("POST", TRYOUTS, lambda c: {"tryout": _tryout(), "outlet": _card(stage="trial")})
    await _open_tryout(harness, ops)

    picker = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == T_PRODUCTS
    assert _curated("staff.sales.tryout.choose_products") in picker.text
    # The two INELIGIBLE rows never reach the agent.
    assert picker.button_labels() == [
        "Pure Water 19L", "Pure Water 10L", f"❌ {_curated('staff.cancel')}",
    ]
    assert _calls(harness, "GET", PRODUCTS)[-1].params == {"page": 1, "per_page": 100}

    await harness.send(ops.tap("staff_sales_t_p_11"))
    assert harness.conversation_state(CONV) == T_QTY
    assert _curated("staff.sales.tryout.quantity_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_t_qty_11_2"))
    basket = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == T_PRODUCTS
    assert "Pure Water 19L · 2" in basket.button_labels()
    assert "• Pure Water 19L × 2" in basket.text

    await harness.send(ops.tap("staff_sales_t_done"))
    assert harness.conversation_state(CONV) == T_NOTES
    assert _curated("staff.sales.tryout.notes_prompt") in harness.telegram.last_shown().text

    await harness.send(ops.text("Owner wants to taste it first"))
    confirm = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == T_CONFIRM
    assert _curated("staff.sales.tryout.confirm_title") in confirm.text
    assert "• Pure Water 19L × 2" in confirm.text
    assert "Owner wants to taste it first" in confirm.text
    assert _curated("staff.sales.tryout.confirm") in " ".join(confirm.button_labels())
    # Nothing is written before Confirm.
    assert _calls(harness, "POST", TRYOUTS) == []
    # Two GETs so far and no more are owed: the card the agent tapped 🧪 on,
    # and the entry point's own ownership proof.
    gets_before_confirm = len(_calls(harness, "GET", f"{OUTLETS}/5"))
    assert gets_before_confirm == 2

    await harness.send(ops.tap("staff_sales_t_confirm"))
    assert [c.data for c in _calls(harness, "POST", TRYOUTS)] == [{
        "items": [{"product_id": 11, "quantity": 2}],
        "notes": "Owner wants to taste it first",
    }]
    receipt = harness.telegram.last_shown()
    assert _rendered("staff.sales.tryout.created", tryout_number="TRY_000077_26") in receipt.text
    assert "• Pure Water 19L × 2" in receipt.text
    assert _curated("staff.sales.tryout.handoff_queued") in receipt.text
    # The receipt IS the card, drawn from the 201's own `outlet` block: the shop
    # is named, the buttons are the card's, and the agent's next move is on the
    # screen already in front of them.
    assert "Bahor market" in receipt.text
    assert "staff_sales_activate_5" in receipt.callback_data()
    assert "staff_sales_hub" in receipt.callback_data()
    # ...and no second fetch bought it. The GET count is unchanged by the
    # POST: a re-fetch here would be a round-trip for an answer the reply
    # already carried.
    assert len(_calls(harness, "GET", f"{OUTLETS}/5")) == gets_before_confirm
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_zero_takes_a_line_back_off_and_continue_disappears_with_it(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    await _open_tryout(harness, ops)

    await harness.send(ops.tap("staff_sales_t_p_12"))
    await harness.send(ops.tap("staff_sales_t_qty_12_3"))
    assert "staff_sales_t_done" in harness.telegram.last_shown().callback_data()

    await harness.send(ops.tap("staff_sales_t_p_12"))
    await harness.send(ops.tap("staff_sales_t_qty_12_0"))
    emptied = harness.telegram.last_shown()
    assert "staff_sales_t_done" not in emptied.callback_data()
    assert "Pure Water 10L · 0" not in emptied.button_labels()
    assert harness.conversation_state(CONV) == T_PRODUCTS


async def test_a_missing_contact_phone_ends_on_the_outlet_not_on_a_dead_confirm(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("POST", TRYOUTS, lambda c: staff_backend_failure(
        "contact phone required", 400, "SALES_TRYOUT_PHONE_REQUIRED"))
    await _open_tryout(harness, ops)
    await harness.send(ops.tap("staff_sales_t_p_11"))
    await harness.send(ops.tap("staff_sales_t_qty_11_1"))
    await harness.send(ops.tap("staff_sales_t_done"))
    await harness.send(ops.tap("staff_sales_t_skip"))
    assert harness.conversation_state(CONV) == T_CONFIRM

    await harness.send(ops.tap("staff_sales_t_confirm"))
    # The refusal is shown with the sentence the activation path already owns …
    assert any(_curated("staff.sales.error.phone_required") in alert
               for alert in _alerts(harness))
    screen = harness.telegram.last_shown()
    # … and the screen points at the ONE place a contact can be added.
    assert _curated("staff.sales.tryout.no_phone") in screen.text
    assert screen.callback_data() == ["staff_sales_outlet_5"]
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]

    # And that button really reaches the card: the conversation is gone, so the
    # hub's group-0 handler answers it.
    await harness.send(ops.tap("staff_sales_outlet_5"))
    assert "Bahor market" in harness.telegram.last_shown().text


async def test_a_refused_basket_lands_back_on_the_basket(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("POST", TRYOUTS, lambda c: staff_backend_failure(
        "items invalid", 400, "SALES_TRYOUT_ITEMS_INVALID"))
    await _open_tryout(harness, ops)
    await harness.send(ops.tap("staff_sales_t_p_11"))
    await harness.send(ops.tap("staff_sales_t_qty_11_1"))
    await harness.send(ops.tap("staff_sales_t_done"))
    await harness.send(ops.tap("staff_sales_t_skip"))
    await harness.send(ops.tap("staff_sales_t_confirm"))

    assert any(_curated("staff.sales.error.tryout_items") in alert
               for alert in _alerts(harness))
    assert harness.conversation_state(CONV) == T_PRODUCTS
    assert "staff_sales_t_p_11" in harness.telegram.last_shown().callback_data()
    # The draft survives the refusal: the quantity is still on the button.
    assert "Pure Water 19L · 1" in harness.telegram.last_shown().button_labels()


async def test_an_empty_catalogue_never_opens_a_basket_that_cannot_be_sent(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    # A basket left over from an earlier try-out, seeded so the assertion below
    # is a real gate rather than a tautology. The entry point's invariant is
    # UNCONDITIONAL: every way out of it -- including the three early returns --
    # leaves no draft behind, because a stale basket plus the next tap is an
    # agent sending products they chose for a different shop.
    harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["sales_tryout"] = {
        "outlet_id": 9, "outlet_name": "Someone else", "products": [{"id": 11, "name": "Stale"}],
        "items": {"11": 4}, "notes": None,
    }
    await _open_tryout(harness, ops, catalogue=[_product(13, "Ice Tea 1L", eligible=False)])

    assert any(_curated("staff.sales.tryout.no_products") in alert
               for alert in _alerts(harness))
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    # The outlet card the agent tapped from is still on screen, buttons intact.
    assert "staff_sales_tryout_5" in harness.telegram.last_shown().callback_data()


async def test_an_active_outlet_is_never_offered_a_tryout(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(stage="active")})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    assert "staff_sales_tryout_5" not in harness.telegram.last_shown().callback_data()


async def test_a_menu_tap_mid_flow_cancels_and_clears(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda c: {"items": []})
    await _open_tryout(harness, ops)
    await harness.send(ops.tap("staff_sales_t_p_11"))
    await harness.send(ops.tap("staff_sales_t_qty_11_2"))
    await harness.send(ops.tap("staff_sales_t_done"))

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text
    assert _calls(harness, "POST", TRYOUTS) == []


async def test_a_stale_quantity_tap_is_answered_without_touching_the_backend(monkeypatch):
    """A suffix the grid never drew, and a product the catalogue never carried.

    The registered pattern is `\\d+_\\d+`, wider than the grid, so the VALUE is
    re-checked in the handler -- and the answer is an acknowledgement, not a
    write and not a re-render.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _open_tryout(harness, ops)
    await harness.send(ops.tap("staff_sales_t_p_11"))
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_t_qty_11_9"))   # 9 is not on the grid
    await harness.send(ops.tap("staff_sales_t_qty_99_2"))   # 99 is not a product
    assert harness.conversation_state(CONV) == T_QTY
    assert harness.telegram.of("editMessageText") == []
    assert harness.backend.calls == []
    assert len(harness.telegram.of("answerCallbackQuery")) == 2


async def test_a_displaced_basket_timing_out_leaves_the_visit_it_lost_to_alone(monkeypatch):
    """The try-out half of the same rule the Nearby journey pins.

    An agent opens a basket, changes their mind and starts a visit from a card:
    PTB leaves this conversation armed at T_PRODUCTS with nothing to end it, and
    five minutes later its timeout fires on an agent standing at a counter. The
    shared timeout copy would drop the visit draft with the basket and announce
    that nothing was saved — false for a visit the server is still holding open.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    armed = ops.tap("staff_sales_tryout_5")
    harness.backend.route("GET", f"{OUTLETS}/5", lambda c: {"outlet": _card(stage="prospect")})
    harness.backend.route("GET", PRODUCTS, lambda c: {"items": list(CATALOGUE)})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    await harness.send(armed)
    assert harness.conversation_state(CONV) == T_PRODUCTS

    await _to_order(harness, ops)
    assert harness.conversation_state(VISIT_CONV) == V_ORDER
    visit_draft = dict(harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][VISIT_FLOW_KEY])
    assert visit_draft.get("stock"), "fixture: the agent has counted a shelf"
    harness.telegram.reset()
    harness.backend.calls.clear()

    verdict = await fire_timeout(harness, CONV, armed)

    # The HANDLER's own verdict: `fire_timeout` pops the conversation afterwards
    # exactly as PTB's job does, so `conversation_state(...) is None` would hold
    # even if this timeout had done nothing at all.
    assert verdict == ConversationHandler.END
    assert harness.conversation_state(VISIT_CONV) == V_ORDER
    assert harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][VISIT_FLOW_KEY] == visit_draft
    assert harness.telegram.shown == []
    assert harness.backend.calls == []


async def test_a_stale_tryout_tap_is_refused_out_loud_while_a_visit_is_open(monkeypatch):
    """🧪 left on a card the agent walked past, tapped mid-visit.

    `start_tryout` is an ENTRY POINT, so PTB offers it every update the live
    visit does not claim; the guard refuses it. The refusal has to SAY so — a
    silent answer leaves the agent tapping a button that cannot work until the
    visit is closed, with nothing on screen to tell them which visit is in the
    way.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_order(harness, ops)
    visit_draft = dict(harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][VISIT_FLOW_KEY])
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_tryout_5"))

    assert _alerts(harness) == [_curated("staff.sales.error.visit_open")]
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][VISIT_FLOW_KEY] == visit_draft
    assert harness.backend.calls == []
    assert harness.telegram.shown == []


async def test_a_basket_button_tapped_after_a_restart_is_answered_not_left_spinning(monkeypatch):
    """PTB keeps conversation state in MEMORY; the agent's buttons live on Telegram.

    A restart empties `_conversations` while every basket button on the agent's
    screen keeps working. Those taps matched no handler at all: the button spun
    and nothing happened, on a screen whose only other move is to walk back out
    through the hub. Nothing was ever written (a try-out exists only after
    Confirm), so the honest answer is the shared "nothing was saved" copy.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    assert harness.conversation_state(CONV) is None
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_t_done"))

    assert [c.params.get("text", "") for c in harness.telegram.of("answerCallbackQuery")] == [
        _curated("staff.flow_timed_out")
    ]
    assert harness.conversation_state(CONV) is None
    assert "sales_tryout" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert harness.telegram.shown == []
    assert harness.backend.calls == []
