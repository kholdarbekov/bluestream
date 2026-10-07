"""A sales agent adds or fixes an outlet's phone from its card, end to end through the real PTB Application (D31).

The reported bug is the starting point: dev outlet #9 was a prospect with no contact phone, and
Request activation kept refusing with nothing on the card to fix it. The card now draws 📞 from
the backend's `can_set_phone`, and the screen behind it PUTs the number. Every PUT body is asserted
whole, because the number the agent TYPES is not the number the bot SENDS: `validate_phone`
normalises it to E.164 first.
"""
import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.sales.new_outlet import FLOW_KEY as NEW_OUTLET_FLOW_KEY, NO_TYPE
from staff_bot.handlers.sales.outlet_phone import SP_PHONE
from staff_bot.handlers.sales.visit import FLOW_KEY as VISIT_FLOW_KEY, V_CHECKIN
from staff_bot.utils.flow_state import SALES_SET_PHONE_FLOW_KEY
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import (
    OUTLETS,
    _agent,
    _alerts,
    _calls,
    _card,
    _curated,
    _label,
    _prospect_card_response,
)
from tests.staff_bot.test_sales_new_outlet_journey import CONV as NEW_OUTLET_CONV
from tests.staff_bot.test_sales_visit_journey import (
    CONV as VISIT_CONV,
    CURRENT as VISIT_CURRENT,
    _outlet_card as _visit_outlet,
    _start as _start_visit,
    _visit,
)
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONV = "staff_sales_set_phone"
CARD = f"{OUTLETS}/5"
PRIMARY_PHONE = f"{OUTLETS}/5/primary-phone"
# What the agent types, and the one number the backend may receive for it.
TYPED, SENT = "90 123 45 67", "+998901234567"
# A second, DIFFERENT valid number, for the retype after a refusal.
RETYPED, RESENT = "93 555 12 34", "+998935551234"
HUB_TITLE = _curated("staff.sales.hub.title")


def _user_data(harness):
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


def _contact(phone, *, name="Olim aka", is_primary=True, contact_id=1):
    """`serialize_outlet_contact`, the shape `_card`'s default contact already has."""
    return {"id": contact_id, "name": name, "phone": phone, "role": "owner",
            "is_primary": is_primary, "presence_window": None}


def _prospect(**over):
    """GET /outlets/5 for the reported outlet: an unlinked grocery prospect with no contact.

    Both readiness flags are the backend's answers (`OutletService.card`) and are set
    explicitly, so no test here leans on a fixture default for the button it asserts.
    """
    card = _prospect_card_response()["outlet"]
    card.update(contacts=[], can_set_phone=True, can_request_activation=False)
    card.update(over)
    return card


def _saved(phone=SENT):
    """The PUT's 200: the card again, the number on a primary contact named after the
    outlet (what `set_primary_phone` creates when there was none), now requestable."""
    return {"outlet": _prospect(contacts=[_contact(phone, name="Bahor market", contact_id=9)],
                                can_request_activation=True)}


def _puts(harness):
    """Every body the bot sent to ANY outlet's /primary-phone, in order."""
    return [call.data for call in harness.backend.calls
            if call.method == "PUT" and call.endpoint.endswith("/primary-phone")]


async def _open_prompt(harness, ops):
    """Open the card and tap 📞 — the only way in. Returns that tap: it is the
    update PTB's timeout job re-offers (`fire_timeout`)."""
    harness.backend.route("GET", CARD, lambda c: {"outlet": _prospect()})
    await harness.send(ops.tap("staff_sales_outlet_5"))
    armed = ops.tap("staff_sales_setphone_5")
    await harness.send(armed)
    return armed


def _resumable_visit(harness):
    """What ⏯ Resume reads: a visit the server still holds open, at its check-in."""
    harness.backend.route("GET", VISIT_CURRENT, lambda c: {"visit": _visit(), "outlet": _visit_outlet()})


# ---- the button --------------------------------------------------------------------


@pytest.mark.parametrize("contacts", [
    pytest.param([], id="no_contact_at_all"),
    pytest.param([_contact(None)], id="a_name_and_no_phone"),
    pytest.param(
        [_contact(None, name="Dilnoza", is_primary=False, contact_id=1),
         _contact(RESENT, name="Botir", is_primary=False, contact_id=2)],
        id="no_primary_flag_so_the_first_contact_speaks",
    ),
])
async def test_a_card_whose_primary_contact_has_no_phone_offers_add_phone(monkeypatch, contacts):
    """The two shapes the walk-in's old Skip left behind, and a legacy row with no primary flag.

    The third case is why the label and the card's contact line share one rule: the first
    contact is the one the card prints, so it is the one whose missing number the label names.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", CARD, lambda c: {"outlet": _prospect(contacts=contacts)})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    assert f"📞 {_curated('staff.sales.card.add_phone')}" in card.button_labels()
    assert f"📞 {_curated('staff.sales.card.change_phone')}" not in card.button_labels()
    assert "staff_sales_setphone_5" in card.callback_data()
    assert "+998" not in card.text
    # The backend says the outlet cannot request activation yet, so the old dead end is gone.
    assert "staff_sales_activate_5" not in card.callback_data()


@pytest.mark.parametrize("stage, callbacks", [
    # A prospect has no visit button.
    pytest.param("prospect", [
        "staff_sales_activate_5", "staff_sales_setphone_5", "staff_sales_tryout_5", "staff_sales_hub",
    ], id="prospect"),
    # A trial shop draws every row, so this is where the order is really pinned:
    # the visit stays first, then 🚀, then 📞.
    pytest.param("trial", [
        "staff_sales_visit_start_5", "staff_sales_activate_5", "staff_sales_setphone_5",
        "staff_sales_tryout_5", "staff_sales_hub",
    ], id="trial"),
])
async def test_a_card_with_a_number_offers_change_phone_in_the_specified_order(monkeypatch, stage, callbacks):
    """Spec §5.2: visit or Resume, Request activation, the phone, the try-out, Navigate, Back.

    Navigate is a URL button, which carries no callback_data.
    """
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", CARD, lambda c: {"outlet": _prospect(
        stage=stage, contacts=[_contact("+998901112266")], can_request_activation=True)})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    assert f"📞 {_curated('staff.sales.card.change_phone')}" in card.button_labels()
    assert f"📞 {_curated('staff.sales.card.add_phone')}" not in card.button_labels()
    assert card.callback_data() == callbacks


@pytest.mark.parametrize("published", [
    pytest.param({"stage": "active", "can_set_phone": False}, id="the_backend_says_no"),
    pytest.param(None, id="not_published_at_all"),
])
async def test_no_phone_button_unless_the_card_publishes_can_set_phone(monkeypatch, published):
    """The bot never works it out from the stage: a prospect card WITHOUT the field
    (an older backend) gets no button either."""
    outlet = _prospect(contacts=[_contact("+998901112266")])
    if published is None:
        del outlet["can_set_phone"]
    else:
        outlet.update(published)
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("GET", CARD, lambda c: {"outlet": outlet})

    await harness.send(ops.tap("staff_sales_outlet_5"))

    card = harness.telegram.last_shown()
    assert "staff_sales_setphone_5" not in card.callback_data()
    assert [label for label in card.button_labels() if label.startswith("📞")] == []


# ---- the screen --------------------------------------------------------------------


async def test_adding_a_phone_puts_the_normalised_number_and_lands_on_a_requestable_card(monkeypatch):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())

    await _open_prompt(harness, ops)

    prompt = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == SP_PHONE
    assert prompt.text == _curated("staff.sales.phone.enter")
    assert prompt.callback_data() == ["staff_sales_sp_cancel"]
    assert prompt.button_labels() == [f"❌ {_curated('staff.cancel')}"]
    assert _user_data(harness)[SALES_SET_PHONE_FLOW_KEY] == {"outlet_id": 5}
    assert _puts(harness) == []

    await harness.send(ops.text(TYPED))

    assert _puts(harness) == [{"phone": SENT}]
    reply = harness.telegram.last_shown()
    assert reply.text.startswith(f"✅ {_curated('staff.sales.phone.saved')}\n\n")
    assert f"{_curated('staff.sales.card.contact')}: Bahor market {SENT}" in reply.text
    # The card the PUT answered with: Request activation is back, and 📞 now says Change.
    assert reply.callback_data() == [
        "staff_sales_activate_5", "staff_sales_setphone_5", "staff_sales_tryout_5", "staff_sales_hub",
    ]
    assert f"📞 {_curated('staff.sales.card.change_phone')}" in reply.button_labels()
    # Drawn from the reply itself: the only GET is the card the agent tapped 📞 on.
    assert len(_calls(harness, "GET", CARD)) == 1
    assert harness.conversation_state(CONV) is None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)


@pytest.mark.parametrize("typed", ["90 123 45 67", "+998 90 123-45-67", "998901234567", "901234567"])
async def test_every_everyday_format_reaches_the_backend_as_one_e164_number(monkeypatch, typed):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    await _open_prompt(harness, ops)

    await harness.send(ops.text(typed))

    assert _puts(harness) == [{"phone": SENT}]
    assert harness.conversation_state(CONV) is None


async def test_a_landline_is_re_prompted_and_never_sent(monkeypatch):
    """The re-prompt says WHICH number is wanted: an office's 71 line is a valid number, just
    not a mobile, so the operator flows' "Invalid phone number format." left nothing to act on."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved(RESENT))
    await _open_prompt(harness, ops)
    harness.backend.calls.clear()

    await harness.send(ops.text("71 123 45 67"))

    assert harness.backend.calls == []
    screen = harness.telegram.last_shown()
    assert screen.text == _curated("staff.sales.error.mobile_required")
    assert screen.callback_data() == ["staff_sales_sp_cancel"]
    assert harness.conversation_state(CONV) == SP_PHONE
    assert _user_data(harness)[SALES_SET_PHONE_FLOW_KEY] == {"outlet_id": 5}

    await harness.send(ops.text(RETYPED))

    assert _puts(harness) == [{"phone": RESENT}]
    assert harness.conversation_state(CONV) is None


@pytest.mark.parametrize("code, key", [
    # Since D31 both sales doors that raise it (create, this PUT) mean "not an Uzbek mobile".
    ("SALES_CONTACT_PHONE_INVALID", "staff.sales.error.mobile_required"),
    ("SALES_OUTLET_PHONE_REQUIRED", "staff.sales.error.phone_required"),
])
async def test_the_backend_refusing_the_number_keeps_the_agent_on_the_phone_step(monkeypatch, code, key):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: staff_backend_failure("refused", 400, code))
    await _open_prompt(harness, ops)

    await harness.send(ops.text(TYPED))

    assert _puts(harness) == [{"phone": SENT}]
    screen = harness.telegram.last_shown()
    assert screen.text == f"❌ {_curated(key)}"
    assert screen.callback_data() == ["staff_sales_sp_cancel"]
    assert harness.conversation_state(CONV) == SP_PHONE
    assert _user_data(harness)[SALES_SET_PHONE_FLOW_KEY] == {"outlet_id": 5}

    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved(RESENT))
    await harness.send(ops.text(RETYPED))

    assert _puts(harness) == [{"phone": SENT}, {"phone": RESENT}]
    assert harness.telegram.last_shown().text.startswith(f"✅ {_curated('staff.sales.phone.saved')}")
    assert harness.conversation_state(CONV) is None


@pytest.mark.parametrize("failure, key", [
    pytest.param(staff_backend_failure("Internal server error", 500),
                 "staff.error.api.service_unavailable", id="a_5xx"),
    pytest.param(staff_backend_failure("Too many requests", 429), "staff.error.api.rate_limited", id="a_429"),
    # No HTTP status at all: the client's retries ran out, in either transport phase.
    pytest.param(staff_backend_failure("Request failed after retries", None),
                 "staff.error.api.service_unavailable", id="never_delivered"),
    pytest.param(staff_backend_failure("Request failed after retries", None, "TRANSPORT_AMBIGUOUS"),
                 "staff.error.api.service_unavailable", id="ambiguous_transport"),
])
async def test_a_failure_that_refused_nothing_keeps_the_agent_on_the_phone_step(monkeypatch, failure, key):
    """No answer, a 429 or a 5xx says nothing about the outlet or the number, so the screen stays
    with its draft and the next number typed is PUT again -- the same idempotent PUT."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: failure)
    await _open_prompt(harness, ops)

    await harness.send(ops.text(TYPED))

    assert _puts(harness) == [{"phone": SENT}]
    screen = harness.telegram.last_shown()
    assert screen.text == f"❌ {_curated(key)}"
    assert screen.callback_data() == ["staff_sales_sp_cancel"]
    assert harness.conversation_state(CONV) == SP_PHONE
    assert _user_data(harness)[SALES_SET_PHONE_FLOW_KEY] == {"outlet_id": 5}
    # Nothing moved on, so there is no card to redraw: the one GET is the card 📞 was tapped on.
    assert len(_calls(harness, "GET", CARD)) == 1

    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved(RESENT))
    await harness.send(ops.text(RETYPED))

    assert _puts(harness) == [{"phone": SENT}, {"phone": RESENT}]
    assert harness.telegram.last_shown().text.startswith(f"✅ {_curated('staff.sales.phone.saved')}")
    assert harness.conversation_state(CONV) is None


async def test_an_edited_message_is_never_sent_as_the_phone(monkeypatch):
    """`filters.TEXT` also matches an EDITED message (PTB tests `effective_message`), so while
    the prompt was open, editing any older message to a valid mobile PUT that number for this
    outlet. The prompt reads new messages only. The PUT is routed to SUCCEED, so a leak is
    recorded, never refused."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    await _open_prompt(harness, ops)
    assert harness.conversation_state(CONV) == SP_PHONE
    harness.backend.calls.clear()

    await harness.send(ops.edited_text(TYPED))

    assert _puts(harness) == []
    assert harness.backend.calls == []


async def test_an_outlet_that_moved_on_ends_the_screen_and_redraws_its_fresh_card(monkeypatch):
    """Review Focus 4: the PUT wrote nothing, the agent is told why, and the card shows where
    the outlet is now."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: staff_backend_failure(
        "Outlet stage does not allow this", 400, "SALES_OUTLET_STAGE_INVALID"))
    await _open_prompt(harness, ops)
    # Activation was requested from another phone while this prompt was open.
    harness.backend.route("GET", CARD, lambda c: {"outlet": _prospect(
        stage="activation_requested", contacts=[_contact("+998901112266")],
        can_set_phone=False, can_request_activation=False)})

    await harness.send(ops.text(TYPED))

    assert _puts(harness) == [{"phone": SENT}]
    assert harness.telegram.texts()[-2] == f"❌ {_curated('staff.sales.error.stage_invalid')}"
    fresh = harness.telegram.last_shown()
    assert _curated("staff.sales.stage.activation_requested") in fresh.text
    assert "staff_sales_setphone_5" not in fresh.callback_data()
    assert len(_calls(harness, "GET", CARD)) == 2
    assert harness.conversation_state(CONV) is None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)

    harness.backend.calls.clear()
    await harness.send(ops.text(RETYPED))
    assert _puts(harness) == []


@pytest.mark.parametrize("status, code, key", [
    (403, "SALES_OUTLET_NOT_ASSIGNED", "staff.error.api.outlet_not_assigned"),
    (404, "SALES_OUTLET_NOT_FOUND", "staff.error.api.outlet_not_found"),
])
async def test_an_outlet_that_is_no_longer_theirs_gets_the_message_and_no_card(
    monkeypatch, status, code, key
):
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: staff_backend_failure("refused", status, code))
    await _open_prompt(harness, ops)

    await harness.send(ops.text(TYPED))

    assert _puts(harness) == [{"phone": SENT}]
    assert harness.telegram.last_shown().text == f"❌ {_curated(key)}"
    # No redraw: a GET for an outlet that is not theirs would only be refused again.
    assert len(_calls(harness, "GET", CARD)) == 1
    assert harness.conversation_state(CONV) is None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)

    harness.backend.calls.clear()
    await harness.send(ops.text(RETYPED))
    assert _puts(harness) == []


# ---- leaving (Review Focus 5) -------------------------------------------------------

ESCAPES = [
    pytest.param(lambda ops, labels: ops.text(_label(labels, "staff.menu.my_outlets")), HUB_TITLE,
                 id="main_menu_tap"),
    pytest.param(lambda ops, labels: ops.command("start"), None, id="start_command"),
    pytest.param(lambda ops, labels: ops.command("cancel"), "Bahor market", id="cancel_command"),
    pytest.param(lambda ops, labels: ops.tap("staff_sales_hub"), HUB_TITLE, id="inline_my_outlets"),
    pytest.param(lambda ops, labels: ops.tap("staff_sales_sp_cancel"), "Bahor market",
                 id="the_screens_own_cancel"),
    pytest.param(lambda ops, labels: ops.tap("staff_sales_outlet_7"), "Chorsu savdo",
                 id="an_older_cards_outlet_button"),
]


@pytest.mark.parametrize("escape, lands_on", ESCAPES)
async def test_every_way_off_the_screen_ends_it_and_the_next_text_is_not_a_phone(
    monkeypatch, escape, lands_on
):
    """Each must END the conversation, not just draw something over it: an armed SP_PHONE
    outranks the text router, so the next number typed anywhere would be PUT as the phone of
    the outlet the agent walked away from. The PUT is routed to SUCCEED on purpose -- a leak
    is then recorded, never refused."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    harness.backend.route(
        "GET", f"{OUTLETS}/7", lambda c: {"outlet": _card(id=7, name="Chorsu savdo", stage="active")}
    )
    await _open_prompt(harness, ops)
    assert harness.conversation_state(CONV) == SP_PHONE

    await harness.send(escape(ops, labels))

    assert harness.conversation_state(CONV) is None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)
    if lands_on is not None:
        assert lands_on in harness.telegram.last_shown().text
    harness.backend.calls.clear()
    await harness.send(ops.text(TYPED))
    assert _puts(harness) == []


async def test_a_tryout_opened_on_top_drops_the_phone_draft(monkeypatch):
    """`_drop_other_sales_drafts` names the 📞 key: a 🧪 tapped on an older card while the
    prompt is open leaves the prompt nothing to send, so even a valid number typed next is
    never PUT for the outlet the agent walked away from."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    harness.backend.route("GET", "/api/v1/products/", lambda c: {"items": [{
        "id": 11, "name": "Pure Water 19L",
        "flags": {"is_active": True}, "inventory": {"is_tryout_eligible": True},
    }]})
    await _open_prompt(harness, ops)

    await harness.send(ops.tap("staff_sales_tryout_5"))

    assert harness.conversation_state("staff_sales_tryout") is not None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)
    await harness.send(ops.text(TYPED))
    assert _puts(harness) == []
    assert harness.conversation_state(CONV) is None


# ---- the two exits PTB hands to a flow registered ahead of this one (ruling C10) ----

# The walk-in and the visit are registered AHEAD of the 📞 screen, so PTB gives
# them these updates and SP_PHONE never sees them: its `menu_escape` cannot end
# it, and it stays armed behind the screen they open. Their entry points drop
# its draft instead (`_drop_other_sales_drafts`). Neither screen reads text --
# the type picker takes a tap, the check-in a pin -- so a number typed there
# falls through to the prompt behind it.
CLAIMED_AHEAD = [
    pytest.param(lambda ops, labels: ops.text(_label(labels, "staff.menu.new_outlet")),
                 NEW_OUTLET_CONV, NO_TYPE, NEW_OUTLET_FLOW_KEY, id="the_new_outlet_menu_button"),
    pytest.param(lambda ops, labels: ops.tap("staff_sales_visit_resume"),
                 VISIT_CONV, V_CHECKIN, VISIT_FLOW_KEY, id="an_older_cards_resume_visit"),
]


@pytest.mark.parametrize("entry, conversation, state, flow_key", CLAIMED_AHEAD)
async def test_a_flow_that_takes_its_tap_first_leaves_the_screen_nothing_to_send(
    monkeypatch, entry, conversation, state, flow_key
):
    """Review Focus 5 for the two exits `menu_escape` never sees. The PUT is
    routed to SUCCEED, so a leak is recorded, never refused."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    _resumable_visit(harness)
    await _open_prompt(harness, ops)

    await harness.send(entry(ops, labels))

    assert harness.conversation_state(conversation) == state
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)
    harness.backend.calls.clear()
    await harness.send(ops.text(TYPED))
    assert _puts(harness) == []
    assert harness.conversation_state(CONV) is None
    # The flow the agent walked into keeps its place and its draft.
    assert harness.conversation_state(conversation) == state
    assert flow_key in _user_data(harness)


@pytest.mark.parametrize("entry, conversation, state, flow_key", CLAIMED_AHEAD)
async def test_the_screens_timeout_stays_silent_under_the_flow_that_displaced_it(
    monkeypatch, entry, conversation, state, flow_key
):
    """Five minutes after 📞, PTB fires this screen's timeout on an agent who is
    now picking a shop type or checking in at a door. With its draft gone it
    ends in silence (`_flow_timeout(own_key=...)`): no "timed out" line, and no
    main menu drawn over the walk-in's or the check-in's own keyboard."""
    harness, ops, labels = await _agent(monkeypatch)
    _resumable_visit(harness)
    armed = await _open_prompt(harness, ops)
    await harness.send(entry(ops, labels))
    displaced_by = dict(_user_data(harness)[flow_key])
    harness.telegram.reset()
    harness.backend.calls.clear()

    verdict = await fire_timeout(harness, CONV, armed)

    assert verdict == ConversationHandler.END
    assert harness.telegram.shown == []
    assert harness.conversation_state(conversation) == state
    assert _user_data(harness)[flow_key] == displaced_by
    assert harness.backend.calls == []


async def test_the_screen_times_out_with_the_shared_copy_and_drops_its_draft(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    armed = await _open_prompt(harness, ops)
    assert harness.conversation_state(CONV) == SP_PHONE
    harness.telegram.reset()

    verdict = await fire_timeout(harness, CONV, armed)

    assert verdict == ConversationHandler.END
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)
    answer = harness.telegram.last_shown()
    assert answer.text == _curated("staff.flow_timed_out")
    assert answer.button_labels() == labels


async def test_the_phone_button_is_refused_out_loud_while_a_visit_is_open(monkeypatch):
    """📞 left on a card the agent walked past, tapped mid-visit: the try-out's rule."""
    harness, ops, _labels = await _agent(monkeypatch)
    await _start_visit(harness, ops)
    visit_state = harness.conversation_state(VISIT_CONV)
    visit_draft = dict(_user_data(harness)[VISIT_FLOW_KEY])
    assert visit_state is not None
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_setphone_5"))

    assert _alerts(harness) == [_curated("staff.sales.error.visit_open")]
    assert harness.conversation_state(CONV) is None
    assert SALES_SET_PHONE_FLOW_KEY not in _user_data(harness)
    assert harness.conversation_state(VISIT_CONV) == visit_state
    assert _user_data(harness)[VISIT_FLOW_KEY] == visit_draft
    assert harness.backend.calls == []
    assert harness.telegram.shown == []


async def test_the_screens_cancel_after_a_restart_is_answered_not_left_spinning(monkeypatch):
    """PTB keeps conversation state in memory; the prompt's ❌ Cancel outlives a restart."""
    harness, ops, _labels = await _agent(monkeypatch)
    assert harness.conversation_state(CONV) is None
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_sp_cancel"))

    assert _alerts(harness) == [_curated("staff.sales.phone.closed")]
    assert harness.conversation_state(CONV) is None
    assert harness.backend.calls == []
    assert harness.telegram.shown == []


async def _save_a_phone(harness, ops):
    await _open_prompt(harness, ops)
    await harness.send(ops.text(TYPED))
    assert harness.telegram.last_shown().text.startswith(f"✅ {_curated('staff.sales.phone.saved')}")


async def _let_the_screen_time_out(harness, ops):
    await fire_timeout(harness, CONV, await _open_prompt(harness, ops))


@pytest.mark.parametrize("end_the_screen", [
    pytest.param(_save_a_phone, id="after_a_successful_save"),
    pytest.param(_let_the_screen_time_out, id="after_the_timeout"),
])
async def test_the_prompts_old_cancel_says_the_screen_is_closed(monkeypatch, end_the_screen):
    """The ❌ stays on the prompt message once the screen ends. Answering "Nothing was saved"
    right under "✅ Phone saved." is false, so the answer is the one true in every case."""
    harness, ops, _labels = await _agent(monkeypatch)
    harness.backend.route("PUT", PRIMARY_PHONE, lambda c: _saved())
    await end_the_screen(harness, ops)
    assert harness.conversation_state(CONV) is None
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_sp_cancel"))

    assert _alerts(harness) == [_curated("staff.sales.phone.closed")]
    assert harness.conversation_state(CONV) is None
    assert harness.backend.calls == []
    assert harness.telegram.shown == []
