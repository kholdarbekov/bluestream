"""The customer bot's side of the duplicate-address guard, driven through the real dispatcher.

Two facts cross from the bot to the backend for this feature:

* every address CREATE says how its point was chosen (`point_source`), because the backend's
  duplicate and pin rules only trust a placed pin (`map_pin` / `gps`), never a typed one;
* the MOVE route (`POST /api/v1/auth/addresses/<id>/move`) is reached through
  `api_client.move_user_address`, the single client method the duplicate prompt will call.

When the backend answers a create with 409 `ADDRESS_POSSIBLE_DUPLICATE`, the bot asks the customer
instead of carrying on (spec 2026-11-18 §6.6): Use the saved address, Move it to this pin (only when
the backend offers it), Save as new, or Cancel.

Assertions are on what reached the backend (`bot.backend.calls`), with the exact body it carried, and
on what the customer saw, in the real seeded copy.
"""

import json

import pytest

# Module-level, before the seed below, so `i18n`, `keyboards` and `config` resolve as the BOT's versions
# (tests/telegram_bot/test_agent_order_callbacks.py explains the sys.path side effect of importing the seed).
import bot as bot_module  # noqa: F401
import api_client as api_client_module
from handlers.profile import ADDRESS_APARTMENT, ADDRESS_DUPLICATE, ADDRESS_TITLE
from handlers.subscriptions import SELECT_PAYMENT

from tests.integration.address_row_builders import address_row, possible_duplicate_body
from tests.telegram_bot.menu_keyboard_helpers import seeded
from tests.telegram_bot.ptb_harness import DEFAULT_CHAT_ID, DEFAULT_USER_ID, backend_failure
from tests.telegram_bot.test_manual_address_entry_journey import (  # noqa: F401  (bot/user fixtures)
    UZ,
    bot,
    creates,
    let_the_dedup_window_lapse,
    state,
    user,
    walk_to_the_geocode_confirmation,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# The pin the customer in the traced session actually dropped.
PIN_LAT = 41.32354
PIN_LNG = 69.241036


async def test_the_create_payload_names_where_its_point_came_from(bot, user):
    """A shared map pin creates the address at its title step, and says so.

    The backend trusts a `map_pin` point for the duplicate and pin-move rules; a create that
    omitted the source would leave those rules unable to tell a placed pin from a typed one.
    """
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    assert bot.conversation_state("address_conversation") == ADDRESS_TITLE

    await bot.send(user.tap("addr_title_home"))

    created = creates(bot)
    assert len(created) == 1, f"expected exactly one create, got {created}"
    assert created[0].data["point_source"] == "map_pin"
    assert created[0].data["latitude"] == PIN_LAT
    assert created[0].data["longitude"] == PIN_LNG


async def test_the_create_payload_says_a_hand_typed_address_was_typed(bot, user):
    """The manual branch (district fallback, geocode-confirmed) never placed a pin: `typed`."""
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_home"))

    created = creates(bot)
    assert len(created) == 1, f"expected exactly one create, got {created}"
    assert created[0].data["point_source"] == "typed"


async def test_the_move_client_posts_to_the_move_route_with_the_body_it_is_given(bot):
    """`move_user_address` is a thin POST to the move route; the body (`point_source`,
    `confirm_duplicate`, the coordinates) travels unchanged, with the customer's token."""
    bot.backend.addresses[63] = address_row(
        63, title="Uy", full_address="Bunyodkor kochasi 15, Chilonzor, Toshkent",
        latitude=41.2811, longitude=69.1839,
    )
    body = {
        "latitude": PIN_LAT,
        "longitude": PIN_LNG,
        "point_source": "map_pin",
        "confirm_duplicate": True,
    }

    response = await api_client_module.api_client.move_user_address("customer-token", 63, body)

    assert response.success is True
    moves = [call for call in bot.backend.calls if call.endpoint.endswith("/move")]
    assert len(moves) == 1, f"expected exactly one move call, got {moves}"
    assert moves[0].method == "POST"
    assert moves[0].endpoint == "/api/v1/auth/addresses/63/move"
    assert moves[0].data == body
    assert moves[0].user_token == "customer-token"


# ---------------------------------------------------------------------------
# The duplicate prompt
# ---------------------------------------------------------------------------

# The copy the prompt and its outcomes put in front of the customer, read from the real seed (uz).
DUPLICATE_COPY_KEYS = (
    "telegram.address.duplicate_prompt_header",
    "telegram.address.duplicate_prompt_line",
    "telegram.address.duplicate_prompt_footer",
    "telegram.address.duplicate_use_button",
    "telegram.address.duplicate_move_button",
    "telegram.address.duplicate_new_button",
    "telegram.address.duplicate_kept",
    "telegram.address.duplicate_moved",
    "telegram.address.pin_with_driver",
    "telegram.address.pin_shared_place",
    "telegram.address.pin_outlet",
    "telegram.address.move_too_far",
    "telegram.error_occurred",
    "telegram.address.flow_timed_out",
    "telegram.common.address",
)


@pytest.fixture(autouse=True)
def duplicate_copy(monkeypatch):
    """Serve the seeded copy beside the manual-entry table the `bot` fixture hands the harness (that table is read
    at lookup time), restored after each test. A key missing from the seed fails here, not on a customer's screen."""
    for key in DUPLICATE_COPY_KEYS:
        monkeypatch.setitem(UZ, key, seeded(key))


# The saved address the customer already has right there, as the backend's 409 names it.
UY = address_row(63, title="Uy", full_address="Chilonzor 12-kv, 5-uy", is_default=True)
APARTMENT_PROMPT = "Xonadon raqamini yozing"


def duplicate_409(move_address_id=63):
    """The backend's answer to a create next to a saved address (address_guard_service.assert_not_duplicate), in the
    envelope tests/contract/test_bot_address_fakes_match_producer.py holds to the live route."""
    return backend_failure(
        "Conflict", 409, data=possible_duplicate_body([UY | {"distance_m": 14}], move_address_id=move_address_id),
    )


def guard_creates(bot, *, move_address_id=63):
    """`POST /auth/addresses` refuses as a possible duplicate until the body confirms it (`confirm_duplicate`
    is JSON true), then creates as usual."""
    def respond(call):
        if (call.data or {}).get("confirm_duplicate") is True:
            return bot.backend._default(call)
        return duplicate_409(move_address_id)

    bot.backend.route("POST", "/api/v1/auth/addresses", respond)


def offer_card_payment(bot):
    """The checkout's payment picker: card only, so drawing it needs no cart quote."""
    bot.backend.route(
        "GET",
        "/api/v1/payments/methods",
        lambda _call: {"data": {"available_methods": [{"method": "click", "is_active": True}]}},
    )


def calls_after(bot, mark):
    return bot.backend.calls[mark:]


def writes(calls):
    return [call for call in calls if call.method in {"POST", "PUT", "PATCH", "DELETE"}]


def user_data(bot):
    return bot.application.user_data[DEFAULT_USER_ID]


def seeded_text(key, **values):
    return seeded(key).format(**values)


async def reach_the_prompt_from_a_pin(bot, user, *, entry="add_new_address", move_address_id=63):
    """Pin flow: the title tap creates the address, and the backend refuses it as a possible duplicate."""
    guard_creates(bot, move_address_id=move_address_id)
    await bot.send(user.tap(entry))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    assert state(bot) == ADDRESS_DUPLICATE, bot.telegram.texts()


async def reach_the_prompt_by_hand(bot, user):
    """Manual flow: the title tap is the save, and a typed point is never offered Move."""
    guard_creates(bot, move_address_id=None)
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_home"))
    assert state(bot) == ADDRESS_DUPLICATE, bot.telegram.texts()


async def test_a_pin_409_at_the_title_step_shows_the_prompt_instead_of_the_apartment_step(bot, user):
    await reach_the_prompt_from_a_pin(bot, user)

    shown = bot.telegram.last_shown()
    assert shown.callback_data() == ["adup_use_63", "adup_move_63", "adup_new", "cancel_address_creation"]
    assert shown.button_labels() == [
        seeded_text("telegram.address.duplicate_use_button", label="Uy"),
        seeded_text("telegram.address.duplicate_move_button", label="Uy"),
        seeded_text("telegram.address.duplicate_new_button"),
        UZ["telegram.cancel"],
    ]
    assert "Uy · ~14 m" in shown.text and "Chilonzor 12-kv, 5-uy" in shown.text and "parse_mode" not in shown.params
    assert [line.strip() for line in shown.text.splitlines()] == [
        seeded_text("telegram.address.duplicate_prompt_header"),
        "Uy · ~14 m",
        "Chilonzor 12-kv, 5-uy",
        seeded_text("telegram.address.duplicate_prompt_footer"),
    ]
    assert APARTMENT_PROMPT not in bot.telegram.texts()
    # One create, no apartment prompt, and no retry behind the customer's back.
    assert len(creates(bot)) == 1
    assert user_data(bot)["address_duplicate_prompt"] == {
        "step": "title",
        "candidates": [UY | {"distance_m": 14}],
        "move_address_id": 63,
        "message_id": shown.params["message_id"],
    }


async def test_move_is_offered_only_when_the_backend_offers_it(bot, user):
    await reach_the_prompt_from_a_pin(bot, user, move_address_id=None)

    assert bot.telegram.last_shown().callback_data() == ["adup_use_63", "adup_new", "cancel_address_creation"]


async def test_use_from_checkout_opens_the_payment_picker_for_that_address_and_writes_nothing(bot, user):
    bot.backend.addresses[63] = UY
    offer_card_payment(bot)
    await reach_the_prompt_from_a_pin(bot, user, entry="add_new_address_checkout")
    mark = len(bot.backend.calls)

    await bot.send(user.tap("adup_use_63"))

    after = calls_after(bot, mark)
    assert writes(after) == [], f"Use writes nothing, got {writes(after)}"
    assert [call.params for call in after if call.endpoint == "/api/v1/payments/methods"] == [
        {"context": "order", "delivery_address_id": 63}
    ]
    assert bot.telegram.last_shown().callback_data() == ["payment_card", "back_to_delivery"]
    assert user_data(bot)["selected_address_id"] == 63
    assert state(bot) is None
    assert not {"temp_address_data", "address_flow_origin", "address_duplicate_prompt"} & set(user_data(bot))


async def test_use_from_my_addresses_says_the_saved_address_was_kept(bot, user):
    await reach_the_prompt_from_a_pin(bot, user)
    mark = len(bot.backend.calls)

    await bot.send(user.tap("adup_use_63"))

    assert writes(calls_after(bot, mark)) == []
    kept = bot.telegram.last_shown()
    assert kept.text == seeded_text("telegram.address.duplicate_kept", label="Uy")
    assert "keyboard" in kept.reply_markup, "the menu keyboard comes back with the answer"
    assert state(bot) is None
    assert not {"temp_address_data", "address_duplicate_prompt"} & set(user_data(bot))


async def test_save_as_new_repeats_the_create_with_the_flag_once_and_continues_to_the_apartment_step(bot, user):
    await reach_the_prompt_from_a_pin(bot, user)

    await bot.send(user.tap("adup_new"))

    posts = creates(bot)
    assert [c.data.get("confirm_duplicate") for c in posts] == [None, True]
    assert posts[1].data == {**posts[0].data, "confirm_duplicate": True}, "the same create, plus the flag only"
    assert state(bot) == ADDRESS_APARTMENT
    assert bot.telegram.last_shown().text == APARTMENT_PROMPT
    temp = user_data(bot)["temp_address_data"]
    assert temp["address_id"] == 900
    draft = json.loads(bot.database.user["bot_state"]).get("address_draft", {})
    assert "confirm_duplicate" not in temp
    assert "confirm_duplicate" not in draft and "confirm_duplicate" not in draft.get("data", {})
    assert "address_duplicate_prompt" not in user_data(bot)


async def test_move_posts_the_bots_move_body_and_resumes_checkout(bot, user):
    """Spec §6.6: on success the bot says the address moved (`duplicate_moved`, on the prompt, which loses its
    buttons), then checkout goes on with the payment picker below it."""
    bot.backend.addresses[63] = UY
    offer_card_payment(bot)
    await reach_the_prompt_from_a_pin(bot, user, entry="add_new_address_checkout")
    prompt = bot.telegram.last_shown()
    mark = len(bot.backend.calls)

    await bot.send(user.tap("adup_move_63"))

    after = calls_after(bot, mark)
    moves = [call for call in after if call.endpoint == "/api/v1/auth/addresses/63/move"]
    assert len(moves) == 1 and moves[0].method == "POST"
    assert moves[0].data == {
        "latitude": PIN_LAT, "longitude": PIN_LNG, "point_source": "map_pin", "confirm_duplicate": True,
    }
    assert writes(after) == moves, "the move is the only write"
    assert [call.params for call in after if call.endpoint == "/api/v1/payments/methods"] == [
        {"context": "order", "delivery_address_id": 63}
    ]
    moved, picker = bot.telegram.shown[-2:]
    assert (moved.method, moved.params["message_id"], moved.text) == (
        "editMessageText", prompt.params["message_id"], seeded_text("telegram.address.duplicate_moved", label="Uy"),
    )
    assert moved.reply_markup == {} and "parse_mode" not in moved.params
    assert (picker.method, picker.callback_data()) == ("sendMessage", ["payment_card", "back_to_delivery"])
    assert user_data(bot)["selected_address_id"] == 63
    assert state(bot) is None


async def test_move_from_my_addresses_says_the_address_moved(bot, user):
    bot.backend.addresses[63] = UY
    await reach_the_prompt_from_a_pin(bot, user)

    await bot.send(user.tap("adup_move_63"))

    moved = bot.telegram.last_shown()
    assert moved.text == seeded_text("telegram.address.duplicate_moved", label="Uy")
    assert "keyboard" in moved.reply_markup, "the menu keyboard comes back with the answer"
    assert (bot.backend.addresses[63]["latitude"], bot.backend.addresses[63]["longitude"]) == (PIN_LAT, PIN_LNG)
    assert state(bot) is None
    assert not {"temp_address_data", "address_duplicate_prompt"} & set(user_data(bot))


# ---------------------------------------------------------------------------
# After the answer: the prompt's buttons, and a tap queued behind the answer
# ---------------------------------------------------------------------------


def acks_seen_by(bot, method, *endpoints):
    """Route each endpoint through a recorder of how many taps the bot had answered when the call arrived; the call
    is then answered as before. Returns the recorded counts."""
    seen = []
    for endpoint in endpoints:
        previous = bot.backend.routes.get((method, endpoint))

        def respond(call, previous=previous):
            seen.append(len(bot.telegram.of("answerCallbackQuery")))
            return previous(call) if previous is not None else bot.backend._default(call)

        bot.backend.route(method, endpoint, respond)
    return seen


def timed_out_alerts(bot):
    timed_out = seeded("telegram.address.flow_timed_out")
    return [call for call in bot.telegram.of("answerCallbackQuery") if timed_out in call.params.get("text", "")]


@pytest.mark.parametrize(
    ("tap", "entry", "method", "endpoints"),
    [
        ("adup_move_63", "add_new_address", "POST", ("/api/v1/auth/addresses/63/move",)),
        ("adup_use_63", "add_new_address_checkout", "GET", ("/api/v1/auth/addresses", "/api/v1/payments/methods")),
    ],
    ids=["move", "use-in-checkout"],
)
async def test_use_and_move_answer_the_tap_before_any_backend_call(bot, user, tap, entry, method, endpoints):
    """The spinner stops at once: left up for the move or the payment picker's calls, it invites a second tap."""
    bot.backend.addresses[63] = UY
    offer_card_payment(bot)
    await reach_the_prompt_from_a_pin(bot, user, entry=entry)
    seen = acks_seen_by(bot, method, *endpoints)
    bot.telegram.reset()

    await bot.send(user.tap(tap))

    assert seen and set(seen) == {1}, f"answers given when each backend call arrived: {seen}"


async def test_after_a_move_the_prompt_loses_its_buttons_and_a_queued_tap_is_answered_quietly(bot, user):
    """A second tap on the prompt, queued behind a slow Move (or sent from a client that still drew the buttons),
    must not tell the customer "Nothing was saved": their address has just moved."""
    bot.backend.addresses[63] = UY
    await reach_the_prompt_from_a_pin(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap("adup_move_63"))

    [stripped] = bot.telegram.of("editMessageReplyMarkup")
    assert stripped.reply_markup == {}, "the answered prompt keeps no buttons"
    assert bot.telegram.last_shown().text == seeded_text("telegram.address.duplicate_moved", label="Uy")
    mark = len(bot.backend.calls)
    bot.telegram.reset()
    let_the_dedup_window_lapse()

    await bot.send(user.tap("adup_use_63"))

    assert timed_out_alerts(bot) == []
    assert len(bot.telegram.visible_answers) == 1, "the queued tap's spinner is stopped"
    assert bot.telegram.of("editMessageReplyMarkup", "editMessageText", "sendMessage") == []
    assert writes(calls_after(bot, mark)) == []


async def test_a_use_tap_queued_behind_use_in_checkout_leaves_the_payment_picker_alone(bot, user):
    """In checkout the prompt BECOMES the payment picker (same message): the expired-form handler stripping "its"
    buttons would take the payment methods off the screen."""
    bot.backend.addresses[63] = UY
    offer_card_payment(bot)
    await reach_the_prompt_from_a_pin(bot, user, entry="add_new_address_checkout")
    await bot.send(user.tap("adup_use_63"))
    assert bot.telegram.last_shown().callback_data() == ["payment_card", "back_to_delivery"]
    bot.telegram.reset()
    let_the_dedup_window_lapse()

    await bot.send(user.tap("adup_use_63"))

    assert timed_out_alerts(bot) == []
    assert len(bot.telegram.visible_answers) == 1
    assert bot.telegram.of("editMessageReplyMarkup", "editMessageText", "sendMessage") == []
    assert user_data(bot)["selected_address_id"] == 63


async def test_a_tap_on_an_older_prompt_still_says_the_form_closed(bot, user):
    """Only the prompt that was answered is quiet: a button on another, long-gone prompt still gets the expired-form
    answer and loses its buttons."""
    bot.backend.addresses[63] = UY
    await reach_the_prompt_from_a_pin(bot, user)
    await bot.send(user.tap("adup_move_63"))
    bot.telegram.reset()
    let_the_dedup_window_lapse()

    await bot.send(user.tap("adup_use_63", message_id=4242))

    assert len(timed_out_alerts(bot)) == 1
    assert len(bot.telegram.of("editMessageReplyMarkup")) == 1


@pytest.mark.parametrize(
    ("candidates", "label"),
    [
        ([address_row(64, title="Ofis", full_address="Bobur 3") | {"distance_m": 9}, UY | {"distance_m": 14}], "Uy"),
        ([], None),
    ],
    ids=["the-moved-row-is-second", "no-candidates"],
)
async def test_the_move_button_names_the_address_it_moves(bot, user, candidates, label):
    """Move is labelled by `move_address_id`, never by position; a 409 without candidates still draws the prompt."""
    bot.backend.route(
        "POST",
        "/api/v1/auth/addresses",
        lambda _call: backend_failure(
            "Conflict", 409, data=possible_duplicate_body(candidates, move_address_id=63),
        ),
    )
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))

    assert state(bot) == ADDRESS_DUPLICATE, bot.telegram.texts()
    shown = bot.telegram.last_shown()
    expected = seeded_text("telegram.address.duplicate_move_button", label=label or seeded("telegram.common.address"))
    assert dict(zip(shown.callback_data(), shown.button_labels()))["adup_move_63"] == expected


@pytest.mark.parametrize(
    ("tap", "method", "endpoint"),
    [
        ("adup_move_63", "POST", "/api/v1/auth/addresses/63/move"),
        ("adup_new", "POST", "/api/v1/auth/addresses"),
    ],
    ids=["move", "save-as-new"],
)
async def test_an_unexpected_failure_at_the_prompt_tears_the_flow_down(bot, user, tap, method, endpoint):
    """Like every other exit: the flow keys and the durable draft go, or the next unsolicited pin would open a new
    address form and typed text would be told the form timed out."""
    await reach_the_prompt_from_a_pin(bot, user)

    def broken(_call):
        raise RuntimeError("backend connection reset")

    bot.backend.route(method, endpoint, broken)

    await bot.send(user.tap(tap))

    assert state(bot) is None
    assert bot.telegram.last_shown().text == seeded("telegram.error_occurred")
    assert not {"temp_address_data", "address_flow_origin", "address_duplicate_prompt"} & set(user_data(bot))
    assert not json.loads(bot.database.user["bot_state"]).get("address_draft")


@pytest.mark.parametrize(
    ("reason", "error_code", "copy_key"),
    [
        ("WITH_DRIVER", "ADDRESS_PIN_WITH_DRIVER", "telegram.address.pin_with_driver"),
        ("SHARED_PLACE", "ADDRESS_PIN_SHARED_PLACE", "telegram.address.pin_shared_place"),
        ("OUTLET", "ADDRESS_PIN_OUTLET", "telegram.address.pin_outlet"),
        ("TOO_FAR", "ADDRESS_MOVE_TOO_FAR", "telegram.address.move_too_far"),
    ],
)
async def test_a_pin_refusal_on_move_shows_its_copy_and_redraws_the_prompt_without_move(
    bot, user, reason, error_code, copy_key
):
    bot.backend.addresses[63] = UY
    refusal_body = {"error_code": error_code, "details": {"reason": reason}}
    bot.backend.route(
        "POST", "/api/v1/auth/addresses/63/move", lambda _call: backend_failure("Bad Request", 400, data=refusal_body),
    )
    await reach_the_prompt_from_a_pin(bot, user)

    await bot.send(user.tap("adup_move_63"))

    refusal, prompt = bot.telegram.shown[-2:]
    assert refusal.text == seeded_text(copy_key, label="Uy")
    assert prompt.callback_data() == ["adup_use_63", "adup_new", "cancel_address_creation"]
    assert "Uy · ~14 m" in prompt.text
    assert state(bot) == ADDRESS_DUPLICATE
    assert user_data(bot)["address_duplicate_prompt"]["move_address_id"] is None
    assert bot.backend.addresses[63] == UY, "a refused move changes nothing"


async def test_a_move_that_fails_otherwise_redraws_the_prompt_without_move(bot, user):
    """Review Focus 5: the address was deleted between the prompt and the tap, or the id is not this customer's."""
    bot.backend.route(
        "POST",
        "/api/v1/auth/addresses/63/move",
        lambda _call: backend_failure("Address not found", 404, data={"error_code": "NOT_FOUND"}),
    )
    await reach_the_prompt_from_a_pin(bot, user)

    await bot.send(user.tap("adup_move_63"))

    failure, prompt = bot.telegram.shown[-2:]
    assert failure.text == seeded("telegram.error_occurred")
    assert prompt.callback_data() == ["adup_use_63", "adup_new", "cancel_address_creation"]
    assert state(bot) == ADDRESS_DUPLICATE


async def test_a_new_pin_at_the_prompt_starts_the_title_step_again(bot, user):
    """The customer drops a better pin instead of answering: the prompt on screen no longer applies."""
    await reach_the_prompt_from_a_pin(bot, user)

    await bot.send(user.location(41.3111, 69.2797))

    assert state(bot) == ADDRESS_TITLE
    assert "address_duplicate_prompt" not in user_data(bot)
    assert user_data(bot)["temp_address_data"]["latitude"] == 41.3111


async def test_cancel_at_the_prompt_ends_the_flow_and_writes_nothing(bot, user):
    await reach_the_prompt_from_a_pin(bot, user)
    mark = len(bot.backend.calls)

    await bot.send(user.tap("cancel_address_creation"))

    assert writes(calls_after(bot, mark)) == []
    assert state(bot) is None
    assert not {"temp_address_data", "address_duplicate_prompt"} & set(user_data(bot))


async def test_the_manual_flow_409_shows_the_prompt_and_never_save_failed(bot, user):
    await reach_the_prompt_by_hand(bot, user)

    shown = bot.telegram.last_shown()
    assert shown.callback_data() == ["adup_use_63", "adup_new", "cancel_address_creation"]
    assert "Uy · ~14 m" in shown.text and "parse_mode" not in shown.params
    assert UZ["telegram.address.save_failed"] not in bot.telegram.texts()
    assert len(creates(bot)) == 1
    assert user_data(bot)["address_duplicate_prompt"]["step"] == "final"


async def test_save_as_new_in_the_manual_flow_finishes_the_address(bot, user):
    await reach_the_prompt_by_hand(bot, user)

    await bot.send(user.tap("adup_new"))

    posts = creates(bot)
    assert [c.data.get("confirm_duplicate") for c in posts] == [None, True]
    assert posts[1].data == {**posts[0].data, "confirm_duplicate": True}
    assert state(bot) is None
    assert bot.telegram.last_shown().text == UZ["telegram.address.saved_successfully"]
    assert not {"temp_address_data", "address_duplicate_prompt"} & set(user_data(bot))


# ---------------------------------------------------------------------------
# A re-pin, typed text, restarts and Telegram's limits
# ---------------------------------------------------------------------------

SUPPORT = "/api/v1/support/messages"
REPIN_LAT = 41.3111
REPIN_LNG = 69.2797


def guard_updates(bot, *, confirmed_ok=True):
    """`PUT /auth/addresses/<id>` refuses as a possible duplicate until the body confirms it. The re-pin of a row
    this flow created is a PUT, and the backend offers no Move for it (the row is the flow's own)."""
    def respond(call):
        if (call.data or {}).get("confirm_duplicate") is True:
            return bot.backend._default(call)
        return duplicate_409(None)

    bot.backend.route("PUT", "/api/v1/auth/addresses/900", respond)


def puts(bot):
    return [c for c in bot.backend.calls if c.method == "PUT" and c.endpoint == "/api/v1/auth/addresses/900"]


def deletes(bot):
    return [c for c in bot.backend.calls if c.method == "DELETE"]


async def reach_the_prompt_by_a_re_pin(bot, user):
    """The first pin saves cleanly (address 900); the customer then drops a second pin next to a saved address."""
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    assert state(bot) == ADDRESS_APARTMENT, bot.telegram.texts()
    guard_updates(bot)
    let_the_dedup_window_lapse()
    await bot.send(user.location(REPIN_LAT, REPIN_LNG))
    await bot.send(user.tap("addr_title_home"))
    assert state(bot) == ADDRESS_DUPLICATE, bot.telegram.texts()


async def test_a_re_pin_next_to_a_saved_address_offers_use_save_as_new_and_cancel(bot, user):
    await reach_the_prompt_by_a_re_pin(bot, user)

    assert bot.telegram.last_shown().callback_data() == ["adup_use_63", "adup_new", "cancel_address_creation"]
    assert len(puts(bot)) == 1 and puts(bot)[0].data["latitude"] == REPIN_LAT


async def test_use_after_a_re_pin_deletes_the_row_the_flow_created(bot, user):
    bot.backend.addresses[63] = UY
    await reach_the_prompt_by_a_re_pin(bot, user)

    await bot.send(user.tap("adup_use_63"))

    assert [(c.method, c.endpoint) for c in deletes(bot)] == [("DELETE", "/api/v1/auth/addresses/900")]
    assert 900 not in bot.backend.addresses and 63 in bot.backend.addresses
    assert state(bot) is None


async def test_save_as_new_after_a_re_pin_repeats_the_put_with_the_flag(bot, user):
    await reach_the_prompt_by_a_re_pin(bot, user)

    await bot.send(user.tap("adup_new"))

    sent = puts(bot)
    assert [c.data.get("confirm_duplicate") for c in sent] == [None, True]
    assert sent[1].data == {**sent[0].data, "confirm_duplicate": True}
    assert state(bot) == ADDRESS_APARTMENT
    assert deletes(bot) == []


async def test_typed_text_at_the_prompt_redraws_it_and_files_nothing(bot, user):
    """Review Focus 1: the durable `address_draft` exists, so without a handler in this state the text falls to
    group 0, which says "flow timed out" and clears the draft while the conversation is alive."""
    await reach_the_prompt_from_a_pin(bot, user)
    assert json.loads(bot.database.user["bot_state"]).get("address_draft"), "the pin flow keeps a durable draft"
    mark = len(bot.backend.calls)
    prompt_before = bot.telegram.last_shown()

    await bot.send(user.text("Chilonzor 12-kv"))

    assert [c for c in calls_after(bot, mark) if c.endpoint == SUPPORT] == []
    assert writes(calls_after(bot, mark)) == []
    timed_out = seeded("telegram.address.flow_timed_out")
    assert not any(timed_out in t for t in bot.telegram.texts()), bot.telegram.texts()
    assert json.loads(bot.database.user["bot_state"]).get("address_draft")
    redrawn = bot.telegram.last_shown()
    assert redrawn is not prompt_before
    assert redrawn.text == prompt_before.text and redrawn.callback_data() == prompt_before.callback_data()
    assert "parse_mode" not in redrawn.params
    assert state(bot) == ADDRESS_DUPLICATE


def sent_message_ids(bot):
    """The message_id Telegram gives each sendMessage from now on, in order (the transport's own answer)."""
    ids = []

    def send(params):
        message = bot.telegram._message(params)
        ids.append(message["message_id"])
        return 200, {"ok": True, "result": message}

    bot.telegram.failures["sendMessage"] = send
    return ids


async def test_a_redrawn_prompt_takes_the_buttons_off_the_copy_it_replaces(bot, user):
    """One live prompt at a time. Left with its buttons, an older copy answers a tap made after Use or Move on the
    newer one with "the form timed out, nothing was saved", while the address has just been used or moved."""
    await reach_the_prompt_from_a_pin(bot, user)
    first = bot.telegram.last_shown()
    bot.telegram.reset()
    sent = sent_message_ids(bot)

    await bot.send(user.text("Chilonzor 12-kv"))
    await bot.send(user.text("Uy"))
    second, third = sent
    bot.telegram.clear_failures()

    redraws = bot.telegram.of("sendMessage")
    assert [call.callback_data() for call in redraws] == [first.callback_data()] * 2
    assert [(call.params["message_id"], call.reply_markup) for call in bot.telegram.of("editMessageReplyMarkup")] == [
        (first.params["message_id"], {}),
        (second, {}),
    ], "each redraw strips the copy before it"
    assert user_data(bot)["address_duplicate_prompt"]["message_id"] == third
    bot.telegram.reset()

    await bot.send(user.tap("adup_use_63", message_id=third))

    assert bot.telegram.last_shown().text == seeded_text("telegram.address.duplicate_kept", label="Uy")
    assert timed_out_alerts(bot) == []


async def test_a_prompt_redrawn_after_a_refused_move_strips_nothing_more(bot, user):
    """The refused Move's notice replaces the tapped prompt, buttons and all, so the redraw below it has no older copy
    left to strip."""
    refusal_body = {"error_code": "ADDRESS_PIN_WITH_DRIVER", "details": {"reason": "WITH_DRIVER"}}
    bot.backend.route(
        "POST", "/api/v1/auth/addresses/63/move", lambda _call: backend_failure("Bad Request", 400, data=refusal_body),
    )
    await reach_the_prompt_from_a_pin(bot, user)
    bot.telegram.reset()

    sent = sent_message_ids(bot)

    await bot.send(user.tap("adup_move_63"))

    assert [call.method for call in bot.telegram.shown] == ["editMessageText", "sendMessage"]
    assert bot.telegram.of("editMessageReplyMarkup") == []
    assert user_data(bot)["address_duplicate_prompt"]["message_id"] == sent[0], "the redraw is the live copy"


async def test_the_prompt_sends_customer_text_verbatim_and_fits_telegram_limits(bot, user):
    """Review Focus 2: markup characters reach Telegram as written (no parse mode); labels are cut; callback_data
    stays within 64 bytes."""
    title, street = "<b>Uy</b> *_x_", "K" * 200
    rows = [
        address_row(63, title=title, full_address=street),
        address_row(64, title="T" * 60, full_address=street),
        address_row(65, title="", full_address=street),
    ]
    bot.backend.route(
        "POST",
        "/api/v1/auth/addresses",
        lambda _call: backend_failure(
            "Conflict", 409,
            data=possible_duplicate_body([r | {"distance_m": 10 + i} for i, r in enumerate(rows)], move_address_id=63),
        ),
    )
    await bot.send(user.tap("add_new_address"))
    await bot.send(user.location(PIN_LAT, PIN_LNG))
    await bot.send(user.tap("addr_title_home"))

    shown = bot.telegram.last_shown()
    assert title in shown.text and street in shown.text
    assert "parse_mode" not in shown.params
    labels = shown.button_labels()
    assert seeded_text("telegram.address.duplicate_use_button", label=title) == labels[0]
    assert seeded_text("telegram.address.duplicate_use_button", label="T" * 24 + "…") == labels[1]
    assert seeded_text("telegram.address.duplicate_move_button", label=title) in labels
    for data in shown.callback_data():
        assert len(data.encode()) <= 64, data


async def test_a_live_prompt_button_is_answered_once_by_the_prompt(bot, user):
    await reach_the_prompt_from_a_pin(bot, user)
    tap = user.tap("adup_use_63")

    acting = [(group, getattr(h, "name", None)) for group, h in bot.handlers_matching(tap)]
    # The group-0 `_address_form_expired` fallback also matches, as for every address button, and the prompt's
    # `_consumes` wrapper stops the update before it: the single answer below is the proof.
    assert acting[0] == (-2, "address_conversation"), acting
    bot.telegram.reset()
    await bot.send(tap)

    assert len(bot.telegram.of("answerCallbackQuery")) == 1


async def test_no_prompt_button_reaches_the_subscription_conversation(bot, user):
    """A customer parked in the subscription flow's payment step taps a prompt button: only the address conversation
    may claim it."""
    await reach_the_prompt_from_a_pin(bot, user)
    bot.conversation("subscription_creation")._conversations[(DEFAULT_CHAT_ID, DEFAULT_USER_ID)] = SELECT_PAYMENT

    for data in ("adup_use_63", "adup_move_63", "adup_new"):
        claimed = [h for _group, h in bot.handlers_matching(user.tap(data))]
        assert claimed, f"{data} lands nowhere"
        assert all(getattr(h, "name", None) != "subscription_creation" for h in claimed), data
