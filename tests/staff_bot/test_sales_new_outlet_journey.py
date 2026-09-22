"""The field onboarding conversation, end to end through the real PTB Application.

The reverse-geocode stubs answer with what the REAL endpoint answers with --
"Mirzo Ulug\u2018bek Tumani" (U+2018, not the apostrophe our own constants use),
"Yashnobod Tumani", captured from live Nominatim -- never a canonical key.
The bot forwards that string VERBATIM, and these tests assert the POST body
carries it unchanged: canonicalising here would be a second copy of a rule
that belongs to `OutletService._validate_common`, which is where
tests/unit/test_outlet_district_canonical.py and
tests/integration/test_staff_sales_outlets_api.py prove the mapping.
"""
import pytest

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from staff_bot.handlers.sales.new_outlet import (
    NO_CLASS,
    NO_CONFIRM,
    NO_CONTACT_NAME,
    NO_CONTACT_PHONE,
    NO_NOTES,
    NO_PIN,
)
from tests.staff_bot.test_sales_hub_journey import (
    LOGIN,
    OUTLETS,
    _agent,
    _alerts,
    _calls,
    _curated,
    _label,
    _outlet,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

DEDUPE = f"{OUTLETS}/dedupe"
REVERSE = "/api/v1/addresses/reverse-geocode"
GEOCODE = "/api/v1/addresses/geocode"
IN_ZONE = (41.3111, 69.2797)
OUT_OF_ZONE = (40.0, 65.0)
CONV = "staff_sales_new_outlet"


async def test_happy_path_with_pin_posts_the_exact_payload(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5-kvartal, 12", "district": "Mirzo Ulug\u2018bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    harness.backend.route("POST", OUTLETS, lambda c: {"outlet": _outlet(id=9, name="Bahor market")})
    harness.backend.route("GET", f"{OUTLETS}/9", lambda c: {"outlet": _outlet(id=9)})

    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    assert _curated("staff.sales.new.choose_type") in harness.telegram.last_shown().text
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Bahor market"))
    assert harness.conversation_state(CONV) == NO_CONTACT_NAME
    await harness.send(ops.text("Olim aka"))
    await harness.send(ops.text("90 111 22 66"))
    prompt = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == NO_PIN
    assert _curated("staff.sales.new.share_pin_button") in prompt.button_labels()

    await harness.send(ops.location(*IN_ZONE))
    assert [c.data for c in _calls(harness, "POST", REVERSE)] == [{"latitude": IN_ZONE[0], "longitude": IN_ZONE[1]}]
    assert harness.conversation_state(CONV) == NO_CLASS
    await harness.send(ops.tap("staff_sales_no_class_B"))
    await harness.send(ops.text("Corner shop"))

    confirm = harness.telegram.last_shown()
    assert harness.conversation_state(CONV) == NO_CONFIRM
    assert "Bahor market" in confirm.text and "+998901112266" in confirm.text and "Chilonzor 5-kvartal, 12" in confirm.text
    assert _calls(harness, "GET", DEDUPE)[-1].params == {"name": "Bahor market", "phone": "+998901112266", "latitude": IN_ZONE[0], "longitude": IN_ZONE[1]}

    await harness.send(ops.tap("staff_sales_no_confirm"))
    assert [c.data for c in _calls(harness, "POST", OUTLETS)] == [{
        "name": "Bahor market", "outlet_type": "grocery_store",
        "contact": {"name": "Olim aka", "phone": "+998901112266", "role": "owner"},
        "latitude": IN_ZONE[0], "longitude": IN_ZONE[1], "address_text": "Chilonzor 5-kvartal, 12", "district": "Mirzo Ulug\u2018bek Tumani",
        "class": "B", "notes": "Corner shop", "force": False, "link_user_id": None,
    }]
    done = harness.telegram.last_shown()
    assert _curated("staff.sales.new.created") in done.text and "Bahor market" in done.text
    assert harness.conversation_state(CONV) is None
    assert "new_outlet" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def test_out_of_zone_pin_is_refused_and_skips_are_honoured(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    harness.backend.route("POST", OUTLETS, lambda c: {"outlet": _outlet(id=9, outlet_type="workplace")})
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_workplace"))
    await harness.send(ops.text("Acme office"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_name"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_phone"))

    await harness.send(ops.location(*OUT_OF_ZONE))
    assert _curated("staff.operator.outside_delivery_area") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == NO_PIN
    assert _calls(harness, "POST", REVERSE) == []

    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Yunusobod 4", "district": "Yashnobod Tumani"})
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    await harness.send(ops.tap("staff_sales_no_confirm"))
    assert _calls(harness, "POST", OUTLETS)[-1].data == {
        "name": "Acme office", "outlet_type": "workplace", "contact": None,
        "latitude": IN_ZONE[0], "longitude": IN_ZONE[1], "address_text": "Yunusobod 4", "district": "Yashnobod Tumani",
        "class": None, "notes": None, "force": False, "link_user_id": None,
    }


async def test_duplicate_candidates_offer_link_or_create_anyway(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Mirzo Ulug\u2018bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": [
        {"kind": "customer", "user_id": 41, "outlet_id": None, "name": "Bahor (Olim)", "phone": "+998901112266", "distance_m": 40, "reason": "name_nearby"},
    ]})
    harness.backend.route("POST", OUTLETS, lambda c: {"outlet": _outlet(id=9, user_id=41, stage="active")})
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Bahor market"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_name"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_phone"))
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.new.duplicates_title") in screen.text and "Bahor (Olim)" in screen.text
    buttons = " ".join(screen.button_labels())
    assert _curated("staff.sales.new.create_anyway") in buttons and "Bahor (Olim)" in buttons

    await harness.send(ops.tap("staff_sales_no_link_41"))
    assert _calls(harness, "POST", OUTLETS)[-1].data["link_user_id"] == 41
    assert harness.conversation_state(CONV) is None


async def test_create_anyway_sets_force(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Mirzo Ulug\u2018bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": [{"kind": "outlet", "user_id": None, "outlet_id": 3, "name": "Bahor", "phone": None, "distance_m": 20, "reason": "name_nearby"}]})
    harness.backend.route("POST", OUTLETS, lambda c: {"outlet": _outlet(id=9)})
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Bahor market"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_name"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_phone"))
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    await harness.send(ops.tap("staff_sales_no_force"))
    assert harness.conversation_state(CONV) == NO_CONFIRM
    await harness.send(ops.tap("staff_sales_no_confirm"))
    assert _calls(harness, "POST", OUTLETS)[-1].data["force"] is True


async def test_menu_tap_mid_flow_cancels_and_clears(monkeypatch):
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda c: {"items": []})
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Half done"))
    await harness.send(ops.text(_label(labels, "staff.menu.my_outlets")))
    assert harness.conversation_state(CONV) is None
    assert "new_outlet" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text


async def _walk_to_pin(harness, ops, labels, outlet_type="grocery_store", name="Bahor market"):
    """Type → name → both contact skips, i.e. the shortest way to the pin step."""
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap(f"staff_sales_no_type_{outlet_type}"))
    await harness.send(ops.text(name))
    await harness.send(ops.tap("staff_sales_no_skip_contact_name"))
    await harness.send(ops.tap("staff_sales_no_skip_contact_phone"))


async def test_a_typed_address_is_geocoded_and_carries_the_flow_forward(monkeypatch):
    """The other half of the pin step, and the one a driver-less agent uses indoors.

    `POST /api/v1/addresses/geocode` answers with latitude/longitude/
    formatted_address and NO district (business_app/api/addresses.py:329-335),
    so the outlet is created without one — asserted here rather than assumed,
    because the pin path DOES carry a district and the two must not be
    confused into thinking this one does too.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", GEOCODE, lambda c: {
        "latitude": IN_ZONE[0], "longitude": IN_ZONE[1], "formatted_address": "Chilonzor 5-kvartal, 12",
    })
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    harness.backend.route("POST", OUTLETS, lambda c: {"outlet": _outlet(id=9)})

    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.text("Chilonzor 5-kvartal 12"))

    assert [c.data for c in _calls(harness, "POST", GEOCODE)] == [{"address": "Chilonzor 5-kvartal 12"}]
    assert harness.conversation_state(CONV) == NO_CLASS

    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    await harness.send(ops.tap("staff_sales_no_confirm"))
    posted = _calls(harness, "POST", OUTLETS)[-1].data
    assert (posted["latitude"], posted["longitude"]) == IN_ZONE
    assert posted["address_text"] == "Chilonzor 5-kvartal, 12"
    assert posted["district"] is None


async def test_a_typed_address_the_geocoder_cannot_place_keeps_the_pin_step(monkeypatch):
    """Both ways the typed path fails: the geocoder says no, and it says
    somewhere we do not deliver. Neither may advance the flow — an outlet
    written without a real pin is a stop no driver can be routed to."""
    harness, ops, labels = await _agent(monkeypatch)
    await _walk_to_pin(harness, ops, labels)

    # 404 — the real endpoint's answer for an address it cannot resolve.
    harness.backend.route("POST", GEOCODE, lambda c: staff_backend_failure("not found", 404))
    await harness.send(ops.text("Nowhere at all"))
    assert _curated("staff.operator.address_not_found") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == NO_PIN

    # Resolved, but outside the delivery polygon.
    harness.backend.route("POST", GEOCODE, lambda c: {
        "latitude": OUT_OF_ZONE[0], "longitude": OUT_OF_ZONE[1], "formatted_address": "Samarkand",
    })
    await harness.send(ops.text("Samarkand"))
    assert _curated("staff.operator.address_not_found") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == NO_PIN
    assert _calls(harness, "GET", DEDUPE) == []


async def test_a_failed_dedupe_lookup_is_not_read_as_no_duplicates(monkeypatch):
    """"We could not ask" is not "there are none".

    Treating a failed dedupe GET as an empty candidate list walked the agent
    straight past the one screen that exists to stop a duplicate, and handed
    them a Confirm whose only possible answer from the backend was a 409.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Mirzo Ulug\u2018bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: staff_backend_failure("boom", 500))
    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))

    assert f"❌ {_curated('staff.error.api.service_unavailable')}" in _alerts(harness)
    assert harness.conversation_state(CONV) == NO_NOTES
    assert _calls(harness, "POST", OUTLETS) == []

    # The step simply repeats once the backend is back.
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    assert harness.conversation_state(CONV) == NO_CONFIRM


async def test_a_duplicate_refusal_offers_the_way_out_instead_of_a_dead_confirm(monkeypatch):
    """A 409 on Confirm used to leave Confirm+Cancel on screen — a button whose
    only outcome is the same 409, forever.

    The bot's `APIResponse` carries no `details`, so the candidates the backend
    matched on are re-fetched and drawn as `duplicate_choice`: link, open, or
    create anyway. Here the agent takes the last one and the create is retried
    with `force`.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Mirzo Ulug\u2018bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    created = []

    def _create(call):
        if not call.data.get("force"):
            return staff_backend_failure("duplicate", 409, "SALES_OUTLET_DUPLICATE")
        created.append(call.data)
        return {"outlet": _outlet(id=9)}

    harness.backend.route("POST", OUTLETS, _create)
    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))

    # The dedupe GET saw nothing; the backend disagreed on write.
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": [
        {"kind": "outlet", "user_id": None, "outlet_id": 3, "name": "Bahor", "phone": None,
         "distance_m": 20, "reason": "name_nearby"},
    ]})
    await harness.send(ops.tap("staff_sales_no_confirm"))

    assert f"❌ {_curated('staff.sales.error.duplicate')}" in _alerts(harness)
    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.new.duplicates_title") in screen.text
    assert "staff_sales_no_force" in screen.callback_data()
    assert "staff_sales_outlet_3" in screen.callback_data()
    assert harness.conversation_state(CONV) == NO_CONFIRM

    await harness.send(ops.tap("staff_sales_no_force"))
    await harness.send(ops.tap("staff_sales_no_confirm"))
    assert [c["force"] for c in created] == [True]
    assert harness.conversation_state(CONV) is None


async def test_the_duplicate_409_names_the_candidates_it_matched_on(monkeypatch):
    """The refusal already carries them, so asking again can only disagree with it.

    `OutletService.create` raises `SALES_OUTLET_DUPLICATE` with
    `details={"candidates": ...}` — `find_duplicates`' own rows, the same
    shape `GET /dedupe` answers with — and the staff client keeps the error
    body on `data` for 409s (api_client.py:415-431). A second lookup runs
    milliseconds later against a table that is being written to: it can name
    an outlet the backend did not match on, or none at all, and then the
    screen explaining the refusal contradicts it.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {
        "formatted_address": "Chilonzor 5", "district": "Mirzo Ulug‘bek Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": []})
    harness.backend.route("POST", OUTLETS, lambda c: staff_backend_failure(
        "Possible duplicate outlet", 409, "SALES_OUTLET_DUPLICATE",
        data={"error_code": "SALES_OUTLET_DUPLICATE", "details": {"candidates": [
            {"kind": "outlet", "user_id": None, "outlet_id": 3, "name": "Bahor",
             "phone": None, "distance_m": 20, "reason": "name_nearby"},
        ]}}))
    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    lookups = len(_calls(harness, "GET", DEDUPE))

    await harness.send(ops.tap("staff_sales_no_confirm"))

    screen = harness.telegram.last_shown()
    assert _curated("staff.sales.new.duplicates_title") in screen.text
    assert "Bahor" in screen.text and "20" in screen.text
    assert "staff_sales_outlet_3" in screen.callback_data()
    assert "staff_sales_no_force" in screen.callback_data()
    # The refusal explained itself; nothing was asked a second time.
    assert len(_calls(harness, "GET", DEDUPE)) == lookups
    assert harness.conversation_state(CONV) == NO_CONFIRM


async def test_the_duplicate_screen_survives_telegram_refusing_the_second_answer(monkeypatch):
    """One update, two `answerCallbackQuery` calls — and Telegram allows one.

    `_handle_api_response_error` answers the Confirm tap with the 409 as an
    alert; `_offer_duplicates` then edits that message, and the edit helper used
    to answer the SAME query again with a bare `answer()`. Telegram rejects that
    ("query is too old and response timeout expired, or query id is invalid"),
    the exception escaped BEFORE the edit, and the agent was left holding the
    Confirm keyboard whose only possible outcome is the same 409 — precisely the
    dead end this screen exists to replace.

    So the second answer is scripted to fail here, and the assertion is that the
    duplicate screen is drawn anyway. The acknowledgement is best-effort; the
    EDIT is the part the agent needs.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Yashnobod Tumani"})
    harness.backend.route("GET", DEDUPE, lambda c: {"candidates": [
        {"kind": "outlet", "user_id": None, "outlet_id": 3, "name": "Bahor", "phone": None,
         "distance_m": 20, "reason": "name_nearby"},
    ]})
    harness.backend.route("POST", OUTLETS, lambda c: staff_backend_failure("duplicate", 409, "SALES_OUTLET_DUPLICATE"))
    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))
    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    harness.telegram.reset()

    def _reject_every_bare_acknowledgement(params):
        """The alert goes through; the second, textless answer is refused —
        which is exactly what Telegram does to a query already answered."""
        if params.get("text"):
            return 200, {"ok": True, "result": True}
        return 400, {
            "ok": False, "error_code": 400,
            "description": "Bad Request: query is too old and response timeout expired",
        }

    harness.telegram.failures["answerCallbackQuery"] = _reject_every_bare_acknowledgement
    errors = []
    harness.application.add_error_handler(lambda _u, ctx: errors.append(ctx.error) and None)

    await harness.send(ops.tap("staff_sales_no_confirm"))
    harness.telegram.clear_failures()

    answers = harness.telegram.of("answerCallbackQuery")
    alerts = [c for c in answers if c.params.get("text")]
    assert len(alerts) == 1 and _curated("staff.sales.error.duplicate") in alerts[0].params["text"]
    # The refused acknowledgement was attempted. Not pinned to a COUNT: PTB's
    # `BadRequest` subclasses `NetworkError`, so `_safe_callback_answer`'s
    # transient-retry branch catches it and tries twice. That is base-handler
    # behaviour, not this flow's contract; what this flow owes the agent is the
    # screen below.
    assert len(answers) > len(alerts)
    assert errors == [], f"the refused acknowledgement escaped the handler: {errors}"

    screen = harness.telegram.last_shown()
    assert screen.method == "editMessageText"
    assert _curated("staff.sales.new.duplicates_title") in screen.text
    assert "staff_sales_no_force" in screen.callback_data()
    assert harness.conversation_state(CONV) == NO_CONFIRM


async def test_an_inline_my_outlets_tap_mid_flow_leaves_the_conversation(monkeypatch):
    """The reply-keyboard label escapes through `menu_escape`; the INLINE button
    reaches no state handler at all, so without a fallback the hub rendered and
    the flow stayed armed behind it for the rest of its five minutes."""
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("GET", OUTLETS, lambda c: {"items": []})
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Half done"))

    await harness.send(ops.tap("staff_sales_hub"))
    assert harness.conversation_state(CONV) is None
    assert "new_outlet" not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    # Ended AND drawn: ending in silence would leave the agent looking at a
    # half-filled form with no way to tell that it is gone.
    assert _curated("staff.sales.hub.title") in harness.telegram.last_shown().text


async def test_a_skip_tap_belonging_to_another_step_does_not_advance_the_flow(monkeypatch):
    """The registered patterns are `\\w+`, so the SUFFIX is the handler's job.

    A stale "Skip" from an earlier message otherwise skipped whatever step the
    agent happened to be on — silently answering a question they were still
    looking at.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Bahor market"))

    await harness.send(ops.tap("staff_sales_no_skip_notes"))
    assert harness.conversation_state(CONV) == NO_CONTACT_NAME

    await harness.send(ops.tap("staff_sales_no_type_workplace"))
    assert harness.conversation_state(CONV) == NO_CONTACT_NAME

    # The right one still works.
    await harness.send(ops.tap("staff_sales_no_skip_contact_name"))
    assert harness.conversation_state(CONV) != NO_CONTACT_NAME


async def test_a_wrong_step_tap_is_acknowledged_so_the_button_stops_spinning(monkeypatch):
    """`_stay` answers the query and returns the SAME state.

    The state half is covered above; this is the ACKNOWLEDGEMENT half, which
    nothing looked at. Without it Telegram spins the agent's button until it
    times out, and they cannot tell a tap that landed on the wrong step from a
    bot that has stopped answering — so they tap again, and again.

    Also pins that the wrong tap costs nothing: no backend call, and the step
    the agent was actually on still accepts its answer afterwards.
    """
    harness, ops, labels = await _agent(monkeypatch)
    await harness.send(ops.text(_label(labels, "staff.menu.new_outlet")))
    await harness.send(ops.tap("staff_sales_no_type_grocery_store"))
    await harness.send(ops.text("Bahor market"))
    assert harness.conversation_state(CONV) == NO_CONTACT_NAME
    harness.telegram.reset()
    harness.backend.calls.clear()

    await harness.send(ops.tap("staff_sales_no_skip_notes"))

    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert harness.backend.calls == []
    assert harness.conversation_state(CONV) == NO_CONTACT_NAME

    await harness.send(ops.text("Olim aka"))
    assert harness.conversation_state(CONV) == NO_CONTACT_PHONE


async def test_a_dead_session_at_the_dedupe_step_ends_the_flow_instead_of_parking_it(monkeypatch):
    """Three things can go wrong at the dedupe lookup and they are not the same thing.

    A failed CALL is retryable, so the flow stays put (asserted above). A missing
    TOKEN is not: the session is gone, nothing the agent taps can succeed, and
    leaving the conversation armed would swallow whatever they type next while
    the login prompt sits above it. So it ends — the same answer
    `receive_pin`, `receive_typed_address` and `_submit` already give.

    Driven through the real seam rather than by patching the handler: the token
    manager hands back nothing and the re-auth login fails, which is what a
    revoked staff session actually looks like.
    """
    harness, ops, labels = await _agent(monkeypatch)
    harness.backend.route("POST", REVERSE, lambda c: {"formatted_address": "Chilonzor 5", "district": "Yashnobod Tumani"})
    await _walk_to_pin(harness, ops, labels)
    await harness.send(ops.location(*IN_ZONE))
    await harness.send(ops.tap("staff_sales_no_class_skip"))

    async def _no_token(*_args, **_kwargs):
        return None

    monkeypatch.setattr(harness.application.bot_data["token_manager"], "get_valid_token", _no_token)
    harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID].pop("access_token", None)
    harness.backend.route("POST", LOGIN, lambda c: staff_backend_failure("no session", 401))

    await harness.send(ops.tap("staff_sales_no_skip_notes"))

    assert _calls(harness, "GET", DEDUPE) == []
    assert harness.conversation_state(CONV) is None
    assert _curated("staff.session_expired") in " ".join(_alerts(harness) + harness.telegram.texts())
