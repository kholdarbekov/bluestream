"""A sales agent opens a visit, checks in at the door and counts the shelf.

Driven through the real staff ``Application`` (tests/staff_bot/ptb_harness.py),
so the WIRING is under test as much as the rendering: the conversation has to
outrank the global LOCATION handler for the pin shared at the shop door, every
inline tap has to reach a registered pattern, and the state the agent lands in
is read back out of ``ConversationHandler._conversations`` rather than
inferred from what was drawn.

The fake backend answers with the REAL response shapes (``serialize_visit``,
``serialize_stock_check``). Every figure these screens print — ``distance_m``,
``in_radius``, ``suggested_qty``, ``rate_per_day``, ``last_order_qty`` — is
computed server-side, so a test that let the bot derive one would be asserting
a rule production must not contain.
"""

import pytest

from staff_bot.handlers.sales.visit import (
    FLOW_KEY,
    V_CHECKIN,
    V_CLOSE,
    V_ORDER,
    V_STOCK,
    V_STOCK_QTY,
)
from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_CHAT_ID,
    DEFAULT_DRIVER_TELEGRAM_ID,
    staff_backend_failure,
)
from tests.staff_bot.test_sales_hub_journey import (
    LOGIN,
    OUTLETS,
    _agent,
    _alerts,
    _calls,
    _card,
    _curated,
    _label,
    _rendered,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONV = "staff_sales_visit"
SALES = "/api/v1/staff/sales"
OUTLET_ID, VISIT_ID = 7, 31
# ONE product vocabulary for both visit journey files:
# tests/staff_bot/test_sales_visit_orders_journey.py imports these and taps
# `staff_sales_v_stock_3` / `_stock_4`, and `open_product` refuses an id that
# is not in the fetched catalogue.
WATER_ID, CUPS_ID = 3, 4

START = f"{OUTLETS}/{OUTLET_ID}/visits"
CURRENT = f"{SALES}/visits/current"
CHECKIN = f"{SALES}/visits/{VISIT_ID}/checkin"
STOCK_CHECK = f"{SALES}/visits/{VISIT_ID}/stock-check"
PRODUCTS = f"{SALES}/stock-check-products"
ABANDON = f"{SALES}/visits/{VISIT_ID}/abandon"

# The shop door, and the accuracy Telegram attaches to a phone pin. The
# DISTANCE is not derived from either — the backend measures it.
DOOR = (41.3111, 69.2797)
ACCURACY = 12.0

PRODUCT_ITEMS = [
    {"id": WATER_ID, "name": "Pure Water 19L", "is_returnable_bottle": True, "min_order_quantity": 2},
    {"id": CUPS_ID, "name": "Aqua 0.5L x12", "is_returnable_bottle": False, "min_order_quantity": 1},
]


def _visit(**over):
    """``serialize_visit`` — the keys the staff API answers with."""
    base = {
        "id": VISIT_ID, "outlet_id": OUTLET_ID, "agent_user_id": 55, "status": "in_progress",
        "planned": True, "current_step": "checkin", "started_at": "2026-09-08T05:10:00+00:00",
        "checkin_at": None, "checkin_latitude": None, "checkin_longitude": None,
        "checkin_accuracy_m": None, "distance_m": None, "in_radius": None, "checkin_skipped": False,
        "ended_at": None, "outcome": None, "no_order_reason": None, "dm_present": None,
        "notes": None, "next_visit_at": None, "order": None, "stock_checks": [],
        # Controller ruling 29: what the LAST completed visit counted. Empty
        # here, so the default fixture is a first-ever visit.
        "previous_stock": [],
    }
    base.update(over)
    return base


def _stock_row(**over):
    """``serialize_stock_check`` — ten keys; the POST reply adds ``last_order_qty``.

    SHARED WITH ``tests/staff_bot/test_sales_visit_orders_journey.py``, and
    the defaults are part of that contract: Task 12's basket assertions are
    computed from ``suggested_qty=20`` and ``last_order_qty=15`` on product 3.
    Keyword-only, so a caller names what it is changing and the row keeps the
    backend's own key names.
    """
    base = {
        "product_id": WATER_ID, "product_name": "Pure Water 19L", "on_hand_qty": 4,
        "empties_qty": None, "is_sold_out": False, "is_low": False,
        "suggested_qty": 20, "accepted_qty": None,
        "rate_per_day": 1.857, "rate_source": "stock_checks", "last_order_qty": 15,
    }
    base.update(over)
    return base


def _outlet_card():
    return _card(id=OUTLET_ID, name="Bahor market", stage="active")


async def _start(harness, ops, *, visit=None, outlet=None):
    """Tap ▶ Start visit on the card, with the two calls the entry point makes routed."""
    current = visit if visit is not None else _visit()
    harness.backend.route("POST", START, lambda _c: {"visit": current})
    harness.backend.route("GET", CURRENT, lambda _c: {"visit": current, "outlet": outlet or _outlet_card()})
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": PRODUCT_ITEMS})
    await harness.send(ops.tap(f"staff_sales_visit_start_{OUTLET_ID}"))


async def _to_stock(harness, ops):
    """Start a visit and skip the check-in — the shortest way to the shelf screen."""
    await _start(harness, ops)
    harness.backend.route(
        "POST", CHECKIN, lambda _c: {"visit": _visit(current_step="stock", checkin_skipped=True)}
    )
    await harness.send(ops.tap("staff_sales_v_skipcheckin"))


def _marked(harness):
    """The quantity buttons the redrawn grid marks as chosen."""
    return [label for label in harness.telegram.last_shown().button_labels() if "•" in label]


async def test_starting_a_visit_asks_for_the_door_pin(monkeypatch):
    """The card's ▶ Start visit opens the conversation on the check-in screen.

    Two messages, deliberately: `request_location` exists only on a REPLY
    keyboard, and a reply keyboard needs its own message. The first carries
    the inline Skip/Abandon pair, the second the location button.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)

    assert harness.conversation_state(CONV) == V_CHECKIN
    assert [call.data for call in _calls(harness, "POST", START)] == [{}]
    shown = harness.telegram.shown
    assert _curated("staff.sales.visit.started") in shown[0].text
    # The outlet name is composed AROUND the label, never interpolated into
    # it (controller ruling 19): the label is seeded bare, and a kwarg a
    # template has no field for is dropped without a word.
    assert "Bahor market" in shown[0].text
    assert shown[0].callback_data() == ["staff_sales_v_skipcheckin", "staff_sales_v_abandon"]
    assert _curated("staff.sales.visit.checkin_prompt") in shown[-1].text
    assert _curated("staff.sales.visit.checkin_button") in shown[-1].button_labels()
    # Nothing about the shelf is fetched until the agent gets there.
    assert _calls(harness, "GET", PRODUCTS) == []


async def test_the_pin_posts_the_exact_body_and_prints_the_backend_distance(monkeypatch):
    """The pin the agent shares at the door, and the distance the SERVER measured.

    `30.4` renders as `30`: the rounding is the bot's only arithmetic here.
    `in_radius` decides which sentence is shown — the bot never compares a
    distance to SALES_GEOFENCE_RADIUS_M.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda _c: {"visit": _visit(
        current_step="stock", checkin_at="2026-09-08T05:12:00+00:00",
        checkin_latitude=DOOR[0], checkin_longitude=DOOR[1], checkin_accuracy_m=ACCURACY,
        distance_m=30.4, in_radius=True,
    )})

    await harness.send(ops.location(*DOOR, horizontal_accuracy=ACCURACY))

    assert [call.data for call in _calls(harness, "POST", CHECKIN)] == [
        {"latitude": DOOR[0], "longitude": DOOR[1], "horizontal_accuracy": ACCURACY}
    ]
    texts = [call.text for call in harness.telegram.shown]
    assert any(_rendered("staff.sales.visit.checkin_ok", distance=30) in text for text in texts)
    # The one-shot location keyboard is handed back as the main menu, and the
    # shelf screen follows in its own message.
    assert _curated("staff.menu.my_outlets") in " ".join(harness.telegram.shown[-2].button_labels())
    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.stock_title") in harness.telegram.last_shown().text
    assert f"staff_sales_v_stock_{WATER_ID}" in harness.telegram.last_shown().callback_data()


async def test_a_pin_outside_the_radius_is_recorded_not_refused(monkeypatch):
    """Out of radius is a FACT about the visit, not a rejection.

    The new-outlet flow refuses a pin outside Tashkent; a check-in must not,
    or an agent standing in a basement with a bad fix cannot record the visit
    they actually made. The backend stores it and says so.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda _c: {"visit": _visit(
        current_step="stock", distance_m=742.6, in_radius=False,
    )})

    await harness.send(ops.location(*DOOR, horizontal_accuracy=ACCURACY))

    texts = [call.text for call in harness.telegram.shown]
    assert any(_rendered("staff.sales.visit.checkin_far", distance=743) in text for text in texts)
    assert harness.conversation_state(CONV) == V_STOCK


async def test_skipping_the_checkin_posts_the_skip_flag_alone(monkeypatch):
    """`Skip` sends `{"skipped": true}` and nothing else — no fabricated pin."""
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route(
        "POST", CHECKIN, lambda _c: {"visit": _visit(current_step="stock", checkin_skipped=True)}
    )

    await harness.send(ops.tap("staff_sales_v_skipcheckin"))

    assert [call.data for call in _calls(harness, "POST", CHECKIN)] == [{"skipped": True}]
    texts = [call.text for call in harness.telegram.shown]
    assert any(_curated("staff.sales.visit.checkin_skipped") in text for text in texts)
    assert harness.conversation_state(CONV) == V_STOCK


async def test_a_second_visit_offers_resume_or_abandon_and_resumes_at_the_server_step(monkeypatch):
    """One open visit per agent is the BACKEND's rule (409).

    `APIResponse` carries no `details`, so the open visit's id and outlet are
    re-read from `GET /visits/current` — which is also what lets the offer
    name the outlet and what `Abandon` needs to know what to abandon. Resume
    then draws whatever step the SERVER says the visit is on, not the step
    the agent happened to leave.
    """
    harness, ops, labels = await _agent(monkeypatch)
    open_visit = _visit(current_step="stock", checkin_skipped=True, stock_checks=[
        _stock_row(on_hand_qty=3, empties_qty=6, is_low=True),
    ])
    harness.backend.route(
        "POST", START, lambda _c: staff_backend_failure("visit open", 409, "SALES_VISIT_ALREADY_OPEN")
    )
    harness.backend.route("GET", CURRENT, lambda _c: {"visit": open_visit, "outlet": _outlet_card()})
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": PRODUCT_ITEMS})

    await harness.send(ops.tap(f"staff_sales_visit_start_{OUTLET_ID}"))

    assert harness.conversation_state(CONV) == V_CHECKIN
    offer = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.resume_or_abandon") in offer.text
    assert "Bahor market" in offer.text
    assert offer.callback_data() == ["staff_sales_v_resume", "staff_sales_v_abandon"]
    # The refusal is the OFFER, not an error dead-end on top of it.
    assert _alerts(harness) == []
    # Nothing is drawn for a screen the agent has not chosen yet.
    assert _calls(harness, "GET", PRODUCTS) == []

    await harness.send(ops.tap("staff_sales_v_resume"))

    assert harness.conversation_state(CONV) == V_STOCK
    resumed = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.stock_title") in resumed.text
    assert f"Pure Water 19L — {_curated('staff.sales.visit.stock_row')}: 3" in resumed.text
    assert "staff_sales_v_stockdone" in resumed.callback_data()


async def test_the_shelf_screens_record_counts_and_post_them_verbatim(monkeypatch):
    """Two products, both counted, then submitted.

    Each tap redraws the SAME screen with the chosen value marked — that
    marker is the only feedback a tap landed, because Telegram redraws an
    identical grid otherwise. The POST body is the items list exactly: the
    products the agent counted, in the backend's own list order.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)

    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    assert harness.conversation_state(CONV) == V_STOCK_QTY
    prompt = harness.telegram.last_shown().text
    assert _curated("staff.sales.visit.stock_qty_prompt") in prompt
    assert "Pure Water 19L" in prompt

    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_3"))
    assert _marked(harness) == ["• 3"]
    await harness.send(ops.tap(f"staff_sales_v_empt_{WATER_ID}_6"))
    assert _marked(harness) == ["• 3", "♻️ • 6"]
    await harness.send(ops.tap(f"staff_sales_v_low_{WATER_ID}"))
    assert any(label.startswith("☑") for label in harness.telegram.last_shown().button_labels())

    await harness.send(ops.tap("staff_sales_v_stockback"))
    assert harness.conversation_state(CONV) == V_STOCK
    assert any(
        label.startswith("Pure Water 19L · 3")
        for label in harness.telegram.last_shown().button_labels()
    )

    # A returnable SKU offers the empties grid; a non-returnable one does not.
    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    assert not [d for d in harness.telegram.last_shown().callback_data() if d.startswith("staff_sales_v_empt_")]
    await harness.send(ops.tap(f"staff_sales_v_qty_{CUPS_ID}_0"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))
    await harness.send(ops.tap("staff_sales_v_stockback"))

    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order"),
        "items": [
            _stock_row(on_hand_qty=3, empties_qty=6, is_low=True, last_order_qty=12),
            _stock_row(product_id=CUPS_ID, product_name="Aqua 0.5L x12", on_hand_qty=0,
                       is_sold_out=True, suggested_qty=None, rate_per_day=None,
                       rate_source="none", last_order_qty=None),
        ],
    })
    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert _calls(harness, "POST", STOCK_CHECK)[-1].data == {"items": [
        {"product_id": WATER_ID, "on_hand_qty": 3, "empties_qty": 6, "is_sold_out": False, "is_low": True},
        {"product_id": CUPS_ID, "on_hand_qty": 0, "empties_qty": None, "is_sold_out": True, "is_low": False},
    ]}

    assert harness.conversation_state(CONV) == V_ORDER
    order = harness.telegram.last_shown()
    # Both lines are COMPOSED: the seeded label, then the backend's own
    # quantity and product name. Nothing here is multiplied or rounded by
    # the bot — `suggested_qty` and `last_order_qty` are printed as given.
    assert f"{_curated('staff.sales.visit.order_suggested_line')}: 20 × Pure Water 19L" in order.text
    assert f"{_curated('staff.sales.visit.order_last_line')}: 12 × Pure Water 19L" in order.text
    assert order.callback_data() == [
        "staff_sales_v_ordersugg", "staff_sales_v_orderedit",
        "staff_sales_v_orderlast", "staff_sales_v_noorder", "staff_sales_v_abandon",
    ]


async def test_a_shelf_screen_with_nothing_counted_offers_no_submit(monkeypatch):
    """`Shelf check done` is drawn only once there is a count to send.

    `StockCheckPayload` requires at least one item, and an empty submission
    teaches the replenishment rule nothing — so the button is absent rather
    than answered with a 400.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)

    assert "staff_sales_v_stockdone" not in harness.telegram.last_shown().callback_data()
    assert _calls(harness, "POST", STOCK_CHECK) == []


async def test_a_new_visit_opens_the_shelf_on_the_last_visits_counts(monkeypatch):
    """Spec *Visit* step 2: "Pre-filled from the previous stock check."

    `previous_stock` is the backend's own answer (controller ruling 29) — the
    rows of this outlet's last COMPLETED visit, not of this one. The agent
    adjusts what changed instead of re-typing a shelf that did not move, and
    the grid opens with that number marked so a single tap is a real edit.

    Only the counts carry over. The flags do NOT: "sold out last time" is a
    fact about last week, and a pre-ticked box is a claim the agent never made.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops, visit=_visit(previous_stock=[
        _stock_row(on_hand_qty=5, empties_qty=2, is_sold_out=True),
    ]))
    harness.backend.route(
        "POST", CHECKIN, lambda _c: {"visit": _visit(current_step="stock", checkin_skipped=True,
                                                     previous_stock=[_stock_row(on_hand_qty=5, empties_qty=2,
                                                                                is_sold_out=True)])}
    )
    await harness.send(ops.tap("staff_sales_v_skipcheckin"))

    shelf = harness.telegram.last_shown()
    assert f"Pure Water 19L — {_curated('staff.sales.visit.stock_row')}: 5" in shelf.text
    # Something is already counted, so the submit button is live.
    assert "staff_sales_v_stockdone" in shelf.callback_data()

    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    assert _marked(harness) == ["• 5", "♻️ • 2"]
    assert not [label for label in harness.telegram.last_shown().button_labels() if label.startswith("☑")]


async def test_junk_taps_are_answered_without_touching_the_backend(monkeypatch):
    """A product that is not on the list, and a quantity that is not on the grid.

    The registered patterns are `\\d+` shapes, so the VALUES are re-checked in
    the handler. A junk tap is acknowledged (the button stops spinning) and
    changes nothing: no write, no re-render, same state.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_v_qty_999_3"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_7"))
    await harness.send(ops.tap("staff_sales_v_soldout_999"))

    assert harness.backend.calls == []
    assert harness.telegram.of("editMessageText") == []
    assert len(harness.telegram.of("answerCallbackQuery")) == 3
    assert harness.conversation_state(CONV) == V_STOCK_QTY


async def test_the_per_line_shelf_grid_can_end_the_visit(monkeypatch):
    """`V_STOCK_QTY` is a screen, and it was the one screen with no way out.

    Its ⬅️ goes back to the shelf overview — which DOES draw Abandon — so the agent could
    always reach the button in two taps. That is not the rule: the visit is server-side
    state, `uq_visits_one_open_per_agent` refuses a second one until this is ended, and an
    agent standing at a counter who wants out should not have to know which screen holds the
    exit. Every screen inside a visit ends it.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))

    grid = harness.telegram.last_shown()
    assert "staff_sales_v_stockback" in grid.callback_data()
    assert "staff_sales_v_abandon" in grid.callback_data()

    harness.backend.route("POST", ABANDON, lambda _c: {"visit": _visit(
        status="abandoned", ended_at="2026-09-08T06:00:00+00:00",
    )})
    await harness.send(ops.tap("staff_sales_v_abandon"))

    # Registered in THIS state, not merely drawn: an unregistered button falls through to
    # `_StaleFlowTap`, which re-renders the server's step and posts nothing.
    assert [call.data for call in _calls(harness, "POST", ABANDON)] == [{}]
    assert harness.conversation_state(CONV) is None
    assert _curated("staff.sales.visit.abandoned") in harness.telegram.last_shown().text


async def test_a_menu_tap_leaves_the_conversation_and_the_visit_open(monkeypatch):
    """Walking out is not abandoning.

    The visit lives on the SERVER; the menu escape drops only the phone-side
    draft. If it abandoned, an agent who checked their cash hub mid-visit
    would come back to a lost check-in.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))

    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert _calls(harness, "POST", ABANDON) == []
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text


async def test_the_abandon_button_is_the_only_thing_that_abandons(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", ABANDON, lambda _c: {"visit": _visit(
        status="abandoned", ended_at="2026-09-08T06:00:00+00:00",
    )})

    await harness.send(ops.tap("staff_sales_v_abandon"))

    assert [call.data for call in _calls(harness, "POST", ABANDON)] == [{}]
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert _curated("staff.sales.visit.abandoned") in harness.telegram.last_shown().text


async def test_a_resume_that_finds_no_open_visit_drops_the_stale_draft(monkeypatch):
    """404 means the SERVER has no visit for this agent, so the phone keeps none.

    `user_data` is warm for the whole chat, so a `sales_visit` left behind
    points every later screen at a visit id the server has already closed —
    and the visit conversation's own stale-tap entry point can walk back into
    it from any old keyboard. The backend answers this branch with a
    machine-readable `SALES_VISIT_NOT_FOUND` precisely so the bot can act on
    it without prose matching.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]

    harness.backend.route("GET", CURRENT, lambda _c: staff_backend_failure(
        "No open visit", 404, error_code="SALES_VISIT_NOT_FOUND"))
    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert any(_curated("staff.error.api.not_found") in alert for alert in _alerts(harness))
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_resume_that_gets_a_visitless_200_drops_the_stale_draft_too(monkeypatch):
    """The other half of the same pop, and the one nothing drove.

    `_load_current` gives up the draft on TWO branches: the coded 404 above,
    and a 200 whose body carries no visit — "a 200 with no visit says the same
    thing as the 404". The second is what a backend change would most easily
    reach (an endpoint that starts answering 200 with `visit: null` rather than
    404 breaks no HTTP contract), and it left `user_data` holding a closed
    visit id for every later screen and for the stale-tap entry point. The
    refusal reads from the bot's own copy, not the backend's prose, because
    there is no error body to render here at all.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]

    harness.backend.route("GET", CURRENT, lambda _c: {"visit": None, "outlet": None})
    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert any(_curated("staff.sales.error.visit_not_open") in alert for alert in _alerts(harness))
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_transport_failure_on_resume_keeps_the_draft(monkeypatch):
    """The complement, and the reason the pop is scoped to the 404.

    "I could not ask" is not "there is nothing to ask about": dropping the
    draft on a 503 would throw away a counted shelf because the network
    blinked, and the counts are the expensive part of the visit.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)

    harness.backend.route("GET", CURRENT, lambda _c: staff_backend_failure("upstream is down"))
    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert any(_curated("staff.error.api.service_unavailable") in alert for alert in _alerts(harness))
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_abandoning_before_the_checkin_hands_the_main_menu_back(monkeypatch):
    """The check-in screen REPLACED the agent's menu with a one-shot prompt.

    `CommonKeyboards.location_request(..., include_cancel=False)` is a
    `one_time_keyboard` ReplyKeyboardMarkup: it takes the persistent main menu
    away and leaves no reply-keyboard escape. The close path already hands it
    back (`_post_close`); abandon did not, so an agent who abandoned at the
    door was left holding a phone with no menu on it.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("POST", ABANDON, lambda _c: {"visit": _visit(status="abandoned")})

    await harness.send(ops.tap("staff_sales_v_abandon"))

    left = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.abandoned") in left.text
    assert _curated("staff.menu.my_outlets") in " ".join(left.button_labels())
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_back_to_main_from_the_checkin_screen_also_restores_the_menu(monkeypatch):
    """...and leaves the visit OPEN, which is the whole point of the button.

    Walking out is not abandoning: the visit is server-side state and Resume
    has to walk back into it. What the agent must not be left with is the
    location prompt and nothing else.

    `staff_back_to_main` is answered TWICE by design (bot.py): the
    conversation's fallback ends the flow in group 0, and a group-1 handler
    then renders the destination for everyone, flow or no flow. That second
    one EDITS the tapped card into the inline main menu, so it is never the
    thing that can hand a reply keyboard back — a reply keyboard cannot ride
    on an edit. Which is why this asserts on the message the fallback itself
    sent, not on the last screen.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _start(harness, ops)

    await harness.send(ops.tap("staff_back_to_main"))

    left = [call for call in harness.telegram.shown if _curated("staff.cancelled") in call.text]
    assert len(left) == 1, harness.telegram.texts()
    # The PERSISTENT menu, not an inline copy of it: `keyboard` is a
    # ReplyKeyboardMarkup, which is the thing the check-in prompt took away.
    assert "keyboard" in left[0].reply_markup
    assert _curated("staff.menu.my_outlets") in " ".join(left[0].button_labels())
    assert _calls(harness, "POST", ABANDON) == []
    assert harness.conversation_state(CONV) is None


async def test_resuming_a_visit_at_the_close_step_lands_on_the_close_screen(monkeypatch):
    """The resume router covers every step, including the one Task 12 fills in.

    An agent who finished the shelf and walked out must come back to the
    closing screen, not to a dead end. No order on this visit, so the outcome
    is still the agent's to give — the with-order branch, where Task 12's
    `_close_step` router skips the outcome prompt because the backend stamps
    it, is pinned by Task 12's own journey file.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda _c: {
        "visit": _visit(current_step="close"),
        "outlet": _outlet_card(),
    })

    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert harness.conversation_state(CONV) == V_CLOSE
    assert _curated("staff.sales.visit.close_outcome_prompt") in harness.telegram.last_shown().text
    assert "staff_sales_v_outcome_no_order" in harness.telegram.last_shown().callback_data()


async def test_a_stale_resume_button_says_so_instead_of_opening_an_empty_flow(monkeypatch):
    """The Resume button on an old card, after the visit was closed elsewhere."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route(
        "GET", CURRENT, lambda _c: staff_backend_failure("no open visit", 404, "SALES_VISIT_NOT_FOUND")
    )

    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert harness.conversation_state(CONV) is None
    assert f"❌ {_curated('staff.error.api.not_found')}" in _alerts(harness)
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_sold_out_on_an_uncounted_product_is_a_counted_zero(monkeypatch):
    """A sold-out shelf IS zero, and it must reach the backend as one.

    `_stock_items` leaves out every product with no `on_hand`, so a Sold out
    tap on a product the agent never gave a number for used to be dropped from
    the POST entirely — while the overview kept decorating that product's
    button with 🚫, so the flag looked recorded on screen and
    `visit_stock_checks` never got a row for it. It is also the single
    highest-value input to the replenishment rate, and the case where it
    matters most is exactly the case where there is nothing to count.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order"),
        "items": [_stock_row(product_id=CUPS_ID, product_name="Aqua 0.5L x12",
                             on_hand_qty=0, is_sold_out=True, suggested_qty=None,
                             rate_per_day=None, rate_source="none", last_order_qty=None)],
    })

    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))

    # The zero is shown back as a zero — the grid marks it, so the agent can
    # see the number the bot is about to send on their behalf.
    assert _marked(harness) == ["• 0"]

    await harness.send(ops.tap("staff_sales_v_stockback"))
    shelf = harness.telegram.last_shown()
    assert f"Aqua 0.5L x12 — {_curated('staff.sales.visit.stock_row')}: 0" in shelf.text
    # Something is counted now, so the submit button is live.
    assert "staff_sales_v_stockdone" in shelf.callback_data()

    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert _calls(harness, "POST", STOCK_CHECK)[-1].data == {"items": [
        {"product_id": CUPS_ID, "on_hand_qty": 0, "empties_qty": None,
         "is_sold_out": True, "is_low": False},
    ]}
    assert harness.conversation_state(CONV) == V_ORDER


async def test_un_tapping_sold_out_takes_back_the_zero_it_set(monkeypatch):
    """The zero is the FLAG's doing, so clearing the flag takes it back.

    `toggle_sold_out` writes `on_hand = 0` so the flag can reach
    `POST /stock-check` at all (`_stock_items` drops every uncounted row). A
    double tap used to leave that zero standing: a count the agent never made,
    posted as fact, on the input the replenishment rate leans on hardest.

    Low is tapped in between because that is the ordinary way in: the flag
    appears in the SAME keyboard row the moment the auto-zero lands, so a
    mis-tap is one button away. Low is a claim ABOUT a count, so withdrawing
    the count has to withdraw the claim with it — otherwise the shelf overview
    decorates the product with a warning the POST body does not carry, which is
    the "flag on an uncounted product" hole the Low guard exists to close.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order", checkin_skipped=True), "items": []})

    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_low_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))
    await harness.send(ops.tap("staff_sales_v_stockback"))

    shelf = harness.telegram.last_shown()
    assert f"Pure Water 19L — {_curated('staff.sales.visit.stock_row')}: 4" in shelf.text
    assert f"Aqua 0.5L x12 — {_curated('staff.sales.visit.stock_row')}: 0" not in shelf.text
    assert [label for label in shelf.button_labels() if "⚠️" in label] == []

    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert [c.data for c in _calls(harness, "POST", STOCK_CHECK)] == [{"items": [
        {"product_id": WATER_ID, "on_hand_qty": 4, "empties_qty": None,
         "is_sold_out": False, "is_low": False},
    ]}]


async def test_a_zero_the_agent_tapped_survives_un_tapping_sold_out(monkeypatch):
    """The other half of the same rule: an explicit 0 is the AGENT's number.

    Once they have tapped 0 on the grid, the shelf really was counted and
    found empty — clearing the flag must leave that count exactly where it is.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order", checkin_skipped=True), "items": []})

    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{CUPS_ID}_0"))
    await harness.send(ops.tap(f"staff_sales_v_soldout_{CUPS_ID}"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert [c.data for c in _calls(harness, "POST", STOCK_CHECK)] == [{"items": [
        {"product_id": CUPS_ID, "on_hand_qty": 0, "empties_qty": None,
         "is_sold_out": False, "is_low": False},
    ]}]


async def test_running_low_is_not_offered_until_the_product_has_a_count(monkeypatch):
    """"Low" is a claim ABOUT a number, so it needs one.

    Unlike Sold out there is no quantity it implies, and a flag with no count
    is what `_stock_items` has to drop. The button is therefore absent until
    the shelf is counted, and a stale tap from an older message is refused for
    the same reason rather than silently written into a row nobody will send.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)

    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    uncounted = harness.telegram.last_shown()
    assert f"staff_sales_v_soldout_{CUPS_ID}" in uncounted.callback_data()
    assert f"staff_sales_v_low_{CUPS_ID}" not in uncounted.callback_data()

    harness.backend.calls.clear()
    await harness.send(ops.tap(f"staff_sales_v_low_{CUPS_ID}"))
    assert harness.backend.calls == []
    flow = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][FLOW_KEY]
    assert flow["stock"][str(CUPS_ID)]["low"] is False
    assert flow["stock"][str(CUPS_ID)]["on_hand"] is None

    await harness.send(ops.tap(f"staff_sales_v_qty_{CUPS_ID}_2"))
    counted = harness.telegram.last_shown()
    assert f"staff_sales_v_low_{CUPS_ID}" in counted.callback_data()

    await harness.send(ops.tap(f"staff_sales_v_low_{CUPS_ID}"))
    assert flow["stock"][str(CUPS_ID)]["low"] is True


async def test_a_visit_with_no_stock_check_products_continues_to_the_order(monkeypatch):
    """The state of every install until an admin flips a switch.

    `products.in_sales_stock_check` is added with `server_default=false` and
    the migration backfills nothing, so on deploy day `GET
    /stock-check-products` answers `[]` for everyone. The shelf screen then
    drew exactly one button — 🚫 Abandon — and every visit dead-ended at step
    2: there is no Close on this step, and *Done* is hidden until something has
    been counted. The empty list is not an error, so it gets a sentence and a
    way forward: `POST /stock-check` accepts an empty count and moves the visit
    to `order`, where *No order* and the whole close path work.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": []})
    harness.backend.route(
        "POST", CHECKIN, lambda _c: {"visit": _visit(current_step="stock", checkin_skipped=True)}
    )
    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order"), "items": [],
    })

    await harness.send(ops.tap("staff_sales_v_skipcheckin"))

    shelf = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.stock_no_products") in shelf.text
    assert shelf.callback_data() == ["staff_sales_v_stockdone", "staff_sales_v_abandon"]
    assert _curated("staff.sales.visit.order_done") in " ".join(shelf.button_labels())
    assert harness.conversation_state(CONV) == V_STOCK

    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert _calls(harness, "POST", STOCK_CHECK)[-1].data == {"items": []}
    assert harness.conversation_state(CONV) == V_ORDER
    assert _curated("staff.sales.visit.order_title") in harness.telegram.last_shown().text


async def test_a_failed_product_fetch_offers_a_retry_not_a_dead_end(monkeypatch):
    """A failed GET is not an empty catalogue, and must not be posted as one.

    `_load_products` is only re-attempted on a fresh render and no button on
    this screen produced one, so a single failed fetch left the agent with
    Abandon as the only move. The alert says what went wrong, the screen keeps
    a Retry that re-fetches, and the Continue button is deliberately NOT drawn:
    posting `items: []` here would record "nothing on this shelf" for a shelf
    nobody managed to look at.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("GET", PRODUCTS, lambda _c: staff_backend_failure("upstream is down"))
    harness.backend.route(
        "POST", CHECKIN, lambda _c: {"visit": _visit(current_step="stock", checkin_skipped=True)}
    )

    await harness.send(ops.tap("staff_sales_v_skipcheckin"))

    assert any(_curated("staff.error.api.service_unavailable") in alert for alert in _alerts(harness))
    shelf = harness.telegram.last_shown()
    assert shelf.callback_data() == ["staff_sales_v_stockretry", "staff_sales_v_abandon"]
    assert _curated("staff.sales.visit.stock_no_products") not in shelf.text
    assert harness.conversation_state(CONV) == V_STOCK

    # A stale Continue tap from an older message must not post an empty count
    # for a catalogue the bot never managed to read.
    await harness.send(ops.tap("staff_sales_v_stockdone"))
    assert _calls(harness, "POST", STOCK_CHECK) == []

    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": PRODUCT_ITEMS})
    await harness.send(ops.tap("staff_sales_v_stockretry"))

    recovered = harness.telegram.last_shown()
    assert f"staff_sales_v_stock_{WATER_ID}" in recovered.callback_data()
    assert "staff_sales_v_stockretry" not in recovered.callback_data()
    assert harness.conversation_state(CONV) == V_STOCK


async def test_a_sku_un_flagged_mid_visit_refreshes_the_shelf(monkeypatch):
    """An admin can un-tick `in_sales_stock_check` while the agent is counting.

    `VisitService.stock_check` then refuses the WHOLE post with
    SALES_STOCK_PRODUCT_INVALID, and the shelf on screen still shows the
    product — so every retry posts it again and the visit cannot leave the
    step. The list is the BACKEND's (that is the whole point of fetching it),
    so the answer is to ask for it again and redraw. The agent also reads
    "that product is not on the list" now, not "that number is not usable":
    the two codes used to share one line of copy.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    await harness.send(ops.tap(f"staff_sales_v_stock_{CUPS_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{CUPS_ID}_2"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    harness.backend.route("POST", STOCK_CHECK, lambda _c: staff_backend_failure(
        "Product is not on the stock-check list", 400,
        error_code="SALES_STOCK_PRODUCT_INVALID"))
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": [PRODUCT_ITEMS[0]]})

    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert any(_curated("staff.sales.error.stock_product") in alert for alert in _alerts(harness))
    shelf = harness.telegram.last_shown()
    assert f"staff_sales_v_stock_{WATER_ID}" in shelf.callback_data()
    assert f"staff_sales_v_stock_{CUPS_ID}" not in shelf.callback_data()
    assert harness.conversation_state(CONV) == V_STOCK

    # ...and the retry carries only what the backend still lists.
    harness.backend.route("POST", STOCK_CHECK, lambda _c: {
        "visit": _visit(current_step="order", checkin_skipped=True), "items": []})
    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert [c.data for c in _calls(harness, "POST", STOCK_CHECK)][-1] == {"items": [
        {"product_id": WATER_ID, "on_hand_qty": 4, "empties_qty": None,
         "is_sold_out": False, "is_low": False},
    ]}


async def test_a_visit_button_tapped_after_a_restart_re_renders_the_servers_step(monkeypatch):
    """PTB keeps conversation state in MEMORY; the agent's buttons are on Telegram.

    A bot restart (a deploy, a crash, an OOM kill) empties `_conversations`
    while every screen an agent is looking at keeps working buttons. Those taps
    matched no handler at all: the button spun and nothing happened, on a visit
    that is still open on the server and still the agent's only way forward.

    The catch-all is the conversation's LAST ENTRY POINT, so its return value
    ARMS the conversation again — a top-level handler could re-draw the screen
    but not re-open the flow, and the next tap would land in the same hole.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda _c: {
        "visit": _visit(current_step="stock", checkin_skipped=True,
                        stock_checks=[_stock_row(on_hand_qty=3)]),
        "outlet": _outlet_card(),
    })
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": PRODUCT_ITEMS})
    assert harness.conversation_state(CONV) is None

    await harness.send(ops.tap("staff_sales_v_stockdone"))

    assert harness.conversation_state(CONV) == V_STOCK
    shelf = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.stock_title") in shelf.text
    assert f"Pure Water 19L — {_curated('staff.sales.visit.stock_row')}: 3" in shelf.text
    # The step came from the SERVER, not from the button that was tapped: the
    # tap said "shelf done" and the visit is still on the shelf.
    assert _calls(harness, "POST", STOCK_CHECK) == []


async def test_a_stale_visit_button_with_nothing_open_says_so(monkeypatch):
    """The other half: the visit was closed or abandoned elsewhere.

    Re-arming the conversation on a visit that no longer exists would park the
    agent on a screen whose every button posts to a dead id.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route(
        "GET", CURRENT, lambda _c: staff_backend_failure("no open visit", 404, "SALES_VISIT_NOT_FOUND")
    )

    await harness.send(ops.tap("staff_sales_v_orderconfirm"))

    assert harness.conversation_state(CONV) is None
    assert f"❌ {_curated('staff.error.api.not_found')}" in _alerts(harness)
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_a_tap_from_an_older_screen_re_renders_instead_of_landing_nowhere(monkeypatch):
    """`allow_reentry=True`, so the catch-all also covers the LIVE conversation.

    Telegram keeps every old keyboard alive. A button whose pattern the CURRENT
    state does not register used to fall through every handler in the bot — the
    per-state `_stay` guards only cover buttons that state draws. The catch-all
    claims exactly those and re-draws the server's step, and it claims nothing
    the live state does claim (which is what keeps `_stay` in charge of the
    stale taps it already handles).
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    assert harness.conversation_state(CONV) == V_STOCK
    # The SERVER's answer is what the re-render draws, so it has to say what
    # the check-in already moved the visit to.
    harness.backend.route("GET", CURRENT, lambda _c: {
        "visit": _visit(current_step="stock", checkin_skipped=True),
        "outlet": _outlet_card(),
    })

    # `staff_sales_v_next_7` belongs to the CLOSE screen and is registered in
    # no other state; the server still says the visit is on the shelf.
    await harness.send(ops.tap("staff_sales_v_next_7"))

    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.stock_title") in harness.telegram.last_shown().text
    assert _calls(harness, "POST", f"{SALES}/visits/{VISIT_ID}/close") == []


async def test_the_stale_tap_guard_reads_ptb_privates_that_still_exist(monkeypatch):
    """`_StaleFlowTap.check_update` reaches into PTB (staff_bot/bot.py:1583).

    It asks `conversation._conversations.get(conversation._get_key(update))`
    whether the live state would claim the tap, and steps aside when it would.
    Both names are PRIVATE to python-telegram-bot (22.3, requirements.txt:35):
    a rename, or a `_get_key` that stopped agreeing with the key
    `_conversations` is really stored under, would silently make the guard read
    `None` for every update -- i.e. claim EVERY visit tap, including the ones
    the live state should handle. Nothing would raise; 28 of the visit journeys
    would simply start re-rendering instead of acting.

    So the pin is the AGREEMENT, not merely the attributes: the key the guard
    builds must be the key PTB filed the armed conversation under.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await _start(harness, ops)
    conv = harness.conversation(CONV)

    # A close-screen button, registered in no other state -- exactly what the
    # guard is asked about.
    tap = ops.tap("staff_sales_v_next_7")
    key = conv._get_key(tap)

    assert key == (DEFAULT_DRIVER_CHAT_ID, DEFAULT_DRIVER_TELEGRAM_ID)
    assert conv._conversations.get(key) == V_CHECKIN
    assert conv.states[V_CHECKIN], "the guard iterates conversation.states[state]"


async def _skip_checkin(harness, ops):
    await harness.send(ops.tap("staff_sales_v_skipcheckin"))


async def _share_pin(harness, ops):
    await harness.send(ops.location(*DOOR, horizontal_accuracy=ACCURACY))


async def _to_counted_shelf(harness, ops):
    """The shelf screen with one product counted — the shape `stock_done` posts."""
    await _to_stock(harness, ops)
    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_4"))
    await harness.send(ops.tap("staff_sales_v_stockback"))


async def _finish_shelf(harness, ops):
    await harness.send(ops.tap("staff_sales_v_stockdone"))


async def _abandon(harness, ops):
    await harness.send(ops.tap("staff_sales_v_abandon"))


def _expire_session(harness):
    """No usable token anywhere: the manager has none, the cached one is gone,
    and the silent re-login the bot tries next is refused."""
    async def _no_token(*_args, **_kwargs):
        return None

    harness.application.bot_data["token_manager"].get_valid_token = _no_token
    harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID].pop("access_token", None)
    harness.backend.route("POST", LOGIN, lambda _c: staff_backend_failure("no session", 401))


@pytest.mark.parametrize(
    "arrive, act",
    [
        (_start, _skip_checkin),
        (_start, _share_pin),
        (_to_counted_shelf, _finish_shelf),
        (_to_stock, _abandon),
    ],
    ids=["skip_checkin", "checkin_pin", "stock_done", "abandon"],
)
async def test_an_expired_session_takes_the_visit_draft_with_the_conversation(monkeypatch, arrive, act):
    """Every visit path that ends on "your session expired" drops the draft.

    The conversation really has ended, and `user_data` outlives it: a draft left
    behind belongs to nobody, keeps `_refused_by_open_visit` refusing Nearby and
    the try-out on behalf of a flow that is over, and points every button still
    on screen at a visit this phone can no longer post to. The visit itself is
    untouched -- `_load_current` rebuilds the draft from the server the moment
    the agent signs back in and taps any visit button.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await arrive(harness, ops)
    assert FLOW_KEY in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    _expire_session(harness)

    await act(harness, ops)

    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
