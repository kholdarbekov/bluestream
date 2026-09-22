"""An agent standing in a market asks what else is on this street.

Driven through the real staff ``Application`` (tests/staff_bot/ptb_harness.py),
so the WIRING is under test as much as the rendering: the conversation has to
outrank the global LOCATION handler for the pin, its list rows have to be
claimed by the conversation rather than by the hub's group-0 handler (which
would draw the card while this flow stayed armed behind it), and the state the
agent lands in is read back out of ``ConversationHandler._conversations``.

Every distance printed here is the BACKEND's ``distance_m`` and every row's
position is the backend's ordering. A test that let the bot measure or sort
would be asserting a rule production must not contain.
"""

import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.sales.nearby import FLOW_KEY, NB_LIST, NB_PIN
from staff_bot.handlers.sales.visit import V_ORDER
from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    staff_backend_failure,
)
from tests.staff_bot.test_sales_hub_journey import (
    OUTLETS,
    _agent,
    _agent_row,
    _alerts,
    _calls,
    _card,
    _curated,
    _label,
    _rendered,
)
from tests.staff_bot.test_sales_visit_journey import CONV as VISIT_CONV, _start, _to_stock
from tests.staff_bot.test_sales_visit_orders_journey import _to_order
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONV = "staff_sales_nearby"
# The neighbour this conversation must never steal from. Nearby registers
# `^staff_sales_nb_\w+$` as an ENTRY POINT (so a tap surviving a restart can
# re-arm it), and an entry point fires for any update the active state does
# not claim — which makes "what happens mid-visit" a journey, not a theory.
VISIT_FLOW_KEY = "sales_visit"
# Where the agent is standing, and the accuracy Telegram attaches to a phone
# pin. Neither distance below is derived from these — the backend measures.
HERE = (41.3111, 69.2797)
ACCURACY = 12.0

# Whole metres: `serialize_outlet_agent_row` publishes `round(distance_m)`, so a
# fractional fixture would describe a body this endpoint cannot answer with.
NEAR = _agent_row(id=5, name="Bahor market", stage="active", distance_m=118, overdue_days=3)
FAR = _agent_row(id=6, name="Nur savdo", stage="prospect", distance_m=2410)


def _user_data(harness):
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def _to_prompt(harness, ops, labels):
    """Hub → 🧭 Nearby, which is where every journey below starts."""
    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    await harness.send(ops.tap("staff_sales_nearby"))


async def test_the_hub_offers_nearby_and_the_tap_asks_for_a_pin(monkeypatch):
    """Two messages, deliberately: `request_location` exists only on a REPLY
    keyboard, and a reply keyboard needs its own message. The first REPLACES
    the hub — its buttons would otherwise stay live behind an armed
    conversation, which is the leak `staff_sales_hub`'s fallback exists for.
    """
    harness, ops, labels = await _agent(monkeypatch)

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    hub = harness.telegram.last_shown()
    assert "staff_sales_nearby" in hub.callback_data()
    assert any(
        label.strip().endswith(_curated("staff.sales.hub.nearby"))
        for label in hub.button_labels()
    )

    await harness.send(ops.tap("staff_sales_nearby"))

    assert harness.conversation_state(CONV) == NB_PIN
    assert FLOW_KEY in _user_data(harness)
    header, prompt = harness.telegram.shown[-2], harness.telegram.shown[-1]
    assert _curated("staff.sales.nearby.title") in header.text
    assert header.callback_data() == ["staff_sales_hub"]
    assert _curated("staff.sales.nearby.pin_prompt") in prompt.text
    assert _curated("staff.sales.nearby.pin_button") in prompt.button_labels()
    # Nothing is asked of the backend until there is a pin to ask with.
    assert _calls(harness, "GET", OUTLETS) == []


async def test_the_pin_asks_with_the_agents_coordinates_and_prints_the_backends_distances(monkeypatch):
    """The whole feature in one journey.

    The query carries the pin and NO pagination (`SALES_NEARBY_LIMIT` decides
    the length); the rows keep the backend's order; `118.4` renders as `118`;
    and an outlet the backend published no distance for still gets its row.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": [NEAR, FAR]})
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.location(*HERE, horizontal_accuracy=ACCURACY))

    assert _calls(harness, "GET", OUTLETS)[-1].params == {
        "scope": "nearby", "lat": HERE[0], "lng": HERE[1],
    }
    assert harness.conversation_state(CONV) == NB_LIST
    # The one-shot location keyboard is handed back as the main menu, and the
    # list follows in its own message.
    handback = harness.telegram.shown[-2]
    assert _curated("staff.sales.nearby.found") in handback.text
    assert _curated("staff.menu.my_outlets") in " ".join(handback.button_labels())
    listing = harness.telegram.last_shown()
    assert _curated("staff.sales.nearby.title") in listing.text
    assert listing.callback_data() == [
        "staff_sales_outlet_5", "staff_sales_outlet_6",
        "staff_sales_nb_again", "staff_sales_hub",
    ]
    near_label, far_label = listing.button_labels()[0], listing.button_labels()[1]
    assert near_label.endswith(_rendered("staff.sales.nearby.distance", distance=118))
    assert far_label.endswith(_rendered("staff.sales.nearby.distance", distance=2410))
    # The due list's own suffix belongs to the due list: this row carries the
    # reason it is in THIS list, and `overdue_days=3` is not it.
    assert _rendered("staff.sales.list.overdue_suffix", days=3) not in near_label


async def test_an_outlet_with_no_measured_distance_keeps_its_row(monkeypatch):
    """None is "not measured", which is not zero metres away."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route(
        "GET", OUTLETS, lambda _c: {"items": [_agent_row(id=6, name="Nur savdo", distance_m=None)]}
    )
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.location(*HERE))

    label = harness.telegram.last_shown().button_labels()[0]
    assert label.endswith("Nur savdo")
    assert _rendered("staff.sales.nearby.distance", distance=0) not in label


async def test_an_empty_answer_says_so_and_still_offers_a_new_pin(monkeypatch):
    """An agent out of town gets a sentence, not a bare title with no rows."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": []})
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.location(*HERE))

    listing = harness.telegram.last_shown()
    assert _curated("staff.sales.list.empty") in listing.text
    assert listing.callback_data() == ["staff_sales_nb_again", "staff_sales_hub"]
    assert harness.conversation_state(CONV) == NB_LIST


async def test_a_row_opens_the_outlet_card_and_leaves_the_conversation(monkeypatch):
    """The row is claimed by THIS conversation, not by the hub's group-0 handler.

    Both would draw the same card — `_show_card` is shared — but only this one
    also ends the flow. Left armed, its LOCATION state would swallow the next
    pin the agent shared for a visit check-in.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": [NEAR]})
    harness.backend.route("GET", f"{OUTLETS}/5", lambda _c: {"outlet": _card(stage="active")})
    await _to_prompt(harness, ops, labels)
    await harness.send(ops.location(*HERE))

    await harness.send(ops.tap("staff_sales_outlet_5"))

    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in _user_data(harness)
    card = harness.telegram.last_shown()
    assert "Bahor market" in card.text
    assert "staff_sales_visit_start_5" in card.callback_data()


async def test_a_new_pin_tap_re_opens_the_prompt_from_the_list(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": [NEAR]})
    await _to_prompt(harness, ops, labels)
    await harness.send(ops.location(*HERE))
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_nb_again"))

    assert harness.conversation_state(CONV) == NB_PIN
    assert _curated("staff.sales.nearby.pin_prompt") in harness.telegram.last_shown().text
    assert _curated("staff.sales.nearby.pin_button") in harness.telegram.last_shown().button_labels()
    # Asking for a pin asks the backend nothing.
    assert _calls(harness, "GET", OUTLETS) == []


async def test_a_new_pin_tapped_after_a_restart_re_opens_the_prompt(monkeypatch):
    """PTB keeps conversation state in MEMORY; the agent's buttons live on Telegram.

    A restart empties `_conversations` while the list on the agent's screen
    keeps working buttons. 📍 New pin is registered as an ENTRY POINT as well
    as in NB_LIST for exactly this: an entry point's return value can re-ARM
    the conversation, and without that the tap lands nowhere and the button
    spins on a screen that is the agent's only way forward.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    assert harness.conversation_state(CONV) is None

    await harness.send(ops.tap("staff_sales_nb_again"))

    assert harness.conversation_state(CONV) == NB_PIN
    assert FLOW_KEY in _user_data(harness)
    assert _curated("staff.sales.nearby.pin_prompt") in harness.telegram.last_shown().text


@pytest.mark.parametrize("stale_tap", ["staff_sales_nb_again", "staff_sales_nearby"])
@pytest.mark.parametrize("arm_visit", [_start, _to_stock], ids=["at_checkin", "after_checkin"])
async def test_a_stale_nearby_tap_cannot_steal_an_armed_visit(monkeypatch, arm_visit, stale_tap):
    """The price of registering Nearby's two literals as ENTRY POINTS, driven.

    An entry point fires for an update the ACTIVE state does not claim, and
    the visit conversation is registered in the same group AHEAD of this one
    but claims only its own `staff_sales_v_*` taps. So the scenario that has
    to be proved, not reasoned about: an agent is mid-visit and taps a stale
    Nearby button — 📍 New pin left on an older list, or 🧭 Nearby left on an
    older hub message, which is the same hole one screen further back. Both
    must be refused, at the check-in and after it: the visit's state and draft
    survive intact, nothing is written, nothing is drawn, and the tap is still
    ANSWERED so the button stops spinning. A prompt that re-armed here would
    replace the agent's keyboard with a location request and file their next
    pin as a search instead of their arrival.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    await arm_visit(harness, ops)
    assert harness.conversation_state(VISIT_CONV) is not None
    visit_state = harness.conversation_state(VISIT_CONV)
    visit_draft = dict(_user_data(harness)[VISIT_FLOW_KEY])
    harness.backend.calls.clear()
    harness.telegram.reset()

    await harness.send(ops.tap(stale_tap))

    assert harness.conversation_state(VISIT_CONV) == visit_state
    assert _user_data(harness)[VISIT_FLOW_KEY] == visit_draft
    assert FLOW_KEY not in _user_data(harness)
    assert harness.conversation_state(CONV) is None
    assert harness.backend.calls == []
    # The tap LANDS, and it SAYS why: an answer with no text at all is
    # indistinguishable from a bot that has stopped, and the agent is left
    # tapping a button that will never work until they close the visit.
    assert _alerts(harness) == [_curated("staff.sales.error.visit_open")]
    # ...and it lands on nothing: the visit's own screen is left exactly as it
    # was, with no pin prompt and no Nearby title drawn over it.
    assert harness.telegram.shown == []


async def test_a_failed_search_keeps_the_location_button_on_screen(monkeypatch):
    """The one-shot keyboard collapsed when the pin was sent.

    Without a re-prompt the agent is left with an error alert and nothing to
    tap — the flow is still armed and still waiting for a location they can no
    longer send in one gesture.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route(
        "GET", OUTLETS,
        lambda _c: staff_backend_failure("service unavailable", 503, None),
    )
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.location(*HERE))

    # A pin is a MESSAGE: `_notify_user` has no callback query to answer, so
    # the refusal is a reply the agent can read on the screen, not a toast.
    assert f"❌ {_curated('staff.error.api.service_unavailable')}" in harness.telegram.texts()
    assert harness.conversation_state(CONV) == NB_PIN
    prompt = harness.telegram.last_shown()
    assert _curated("staff.sales.nearby.pin_prompt") in prompt.text
    assert _curated("staff.sales.nearby.pin_button") in prompt.button_labels()


async def test_a_pin_the_backend_refuses_names_the_pin(monkeypatch):
    """`SALES_NEARBY_PIN_REQUIRED` is mapped, so the refusal is a sentence
    about the pin rather than the generic 400 copy."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route(
        "GET", OUTLETS,
        lambda _c: staff_backend_failure("pin required", 400, "SALES_NEARBY_PIN_REQUIRED"),
    )
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.location(*HERE))

    texts = harness.telegram.texts()
    assert f"❌ {_curated('staff.sales.error.pin_required')}" in texts
    assert f"❌ {_curated('staff.error.api.validation')}" not in texts


async def test_a_menu_tap_leaves_nearby_and_clears_the_draft(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    await _to_prompt(harness, ops, labels)

    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))

    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in _user_data(harness)
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text


async def test_a_pin_shared_after_walking_out_is_not_swallowed_by_nearby(monkeypatch):
    """The escape's real cost, driven rather than reasoned about.

    Nearby's NB_PIN is a LOCATION state registered ahead of the bot's global
    location handler. If a menu tap left it armed, the next pin the agent
    shared — for a delivery position, for a check-in — would be filed as a
    Nearby search and answered with a list of shops.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": [NEAR]})
    await _to_prompt(harness, ops, labels)
    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    harness.backend.calls.clear()

    await harness.send(ops.location(*HERE))

    assert _calls(harness, "GET", OUTLETS) == []


async def test_the_list_speaks_the_agents_language(monkeypatch):
    """In English a missing row is invisible: `humanise_key` turns
    `staff.sales.nearby.title` into "Title", which reads like copy. In Russian
    it does not — and the distance UNIT is the row a reader skims past."""
    harness, ops, _labels = await _agent(monkeypatch, language="ru")
    harness.backend.route("GET", OUTLETS, lambda _c: {"items": [NEAR]})
    # Straight to the hub row: `_label` reads the EN catalogue, and this
    # agent's menu is Russian.
    await harness.send(ops.tap("staff_sales_nearby"))

    await harness.send(ops.location(*HERE))

    listing = harness.telegram.last_shown()
    assert _curated("staff.sales.nearby.title", "ru") in listing.text
    assert listing.button_labels()[0].endswith(
        _rendered("staff.sales.nearby.distance", "ru", distance=118)
    )
    assert _rendered("staff.sales.nearby.distance", "en", distance=118) not in listing.button_labels()[0]
    assert _curated("staff.sales.nearby.again", "ru") in " ".join(listing.button_labels())


async def test_a_displaced_nearby_timing_out_leaves_the_visit_it_lost_to_alone(monkeypatch):
    """Two sales conversations really can be armed, and the loser owns ONE key.

    PTB checks an inactive conversation's entry points on every update, so an
    agent who taps 🧭 Nearby and then walks into a visit from a card leaves
    Nearby parked at NB_PIN with nothing to end it. Five minutes later its
    timeout fires — on an agent standing at a counter with a basket on screen.
    A timeout that cleared the whole `PENDING_FLOW_USER_DATA_KEYS` set would
    take the visit draft and the basket with it and announce that nothing was
    saved, which is false twice over: the server is still holding the visit.

    So the displaced conversation's timeout drops its OWN key, and says
    nothing at all when that key is already gone.
    """
    harness, ops, labels = await _agent(monkeypatch)
    armed = ops.tap("staff_sales_nearby")
    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    await harness.send(armed)
    assert harness.conversation_state(CONV) == NB_PIN

    await _to_order(harness, ops)
    assert harness.conversation_state(VISIT_CONV) == V_ORDER
    visit_draft = dict(_user_data(harness)[VISIT_FLOW_KEY])
    assert visit_draft.get("stock"), "fixture: the agent has counted a shelf"
    harness.telegram.reset()
    harness.backend.calls.clear()

    verdict = await fire_timeout(harness, CONV, armed)

    # The HANDLER's own verdict: `fire_timeout` pops the conversation afterwards
    # exactly as PTB's job does, so `conversation_state(...) is None` would hold
    # even if this timeout had done nothing at all.
    assert verdict == ConversationHandler.END
    assert harness.conversation_state(VISIT_CONV) == V_ORDER
    assert _user_data(harness)[VISIT_FLOW_KEY] == visit_draft
    # Silent: the agent is mid-visit and this timer belongs to a conversation
    # they left minutes ago.
    assert harness.telegram.shown == []
    assert harness.backend.calls == []
