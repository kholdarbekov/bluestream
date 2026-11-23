"""Edit location on a saved address, driven through the real dispatcher (spec 2026-11-18 §6.6, D4).

"📍 Edit location" asks for a pin that MOVES the saved address it was tapped on. The prompt is durable
(`bot_state.awaiting_input == 'relocate_address'`, with the address id, its label and when it was armed), so a pin
sent after a restart still moves the right address. Every add-address entry and every flow exit disarms it, so a pin
meant for a new address never moves an old one, and a stale prompt says so and neither moves nor adds.

Assertions are on what reached the backend (`bot.backend.calls`), with the exact body it carried, on the durable
`bot_state` row, and on what the customer saw, in the real seeded copy.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

# Module-level, before the seed below, so `i18n`, `keyboards` and `config` resolve as the BOT's versions
# (tests/telegram_bot/test_agent_order_callbacks.py explains the sys.path side effect of importing the seed).
import bot as bot_module  # noqa: F401
from handlers.profile import ADDRESS_TITLE, PIN_REFUSAL_COPY
from utils import AWAITING_LOCATION_STALE_MINUTES

from tests.integration.address_row_builders import address_row, possible_duplicate_body
from tests.telegram_bot.menu_keyboard_helpers import menu_sends, seeded
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, backend_failure
from tests.telegram_bot.test_address_duplicate_prompt_journey import acks_seen_by
from tests.telegram_bot.test_armed_prompts_survive_a_restart import restart
from tests.telegram_bot.test_manual_address_entry_journey import (  # noqa: F401  (bot/user fixtures)
    CANCEL_BUTTON,
    UZ,
    bot,
    creates,
    let_the_dedup_window_lapse,
    state,
    support_posts,
    user,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# The new pin, inside the delivery zone, and one outside it.
LAT, LNG = 41.32354, 69.241036
OUTSIDE_LAT, OUTSIDE_LNG = 40.1, 65.4

UY = address_row(
    91, title="Uy", full_address="Chilonzor 12-kv, 5-uy", latitude=41.3111, longitude=69.2797, is_default=True,
)
ISH = address_row(92, title="Ish", full_address="Amir Temur 7, Mirobod", latitude=41.3002, longitude=69.2795)

SUPPORT_LABEL = seeded("telegram.menu.support")

# The copy these screens put in front of the customer, read from the real seed (uz).
EDIT_LOCATION_COPY_KEYS = (
    "telegram.address.location_edit_prompt",
    "telegram.address.location_edit_expired",
    "telegram.address.location_updated",
    "telegram.address.location_received",
    "telegram.address.location_edit_send_pin",
    "telegram.address.duplicate_prompt_header",
    "telegram.address.duplicate_prompt_line",
    "telegram.address.duplicate_save_here_button",
    "telegram.address.pin_with_driver",
    "telegram.address.pin_shared_place",
    "telegram.address.pin_outlet",
    "telegram.address.move_too_far",
    "telegram.action_cancelled",
    "telegram.error_occurred",
    "telegram.menu.support",
)


@pytest.fixture(autouse=True)
def edit_location_copy(monkeypatch):
    """Serve the seeded copy beside the manual-entry table the `bot` fixture hands the harness (read at lookup
    time), restored after each test. A key missing from the seed fails here, not on a customer's screen."""
    for key in EDIT_LOCATION_COPY_KEYS:
        monkeypatch.setitem(UZ, key, seeded(key))


@pytest.fixture(autouse=True)
def saved_addresses(bot):
    bot.backend.addresses.update({91: UY, 92: ISH})


def seeded_text(key, **values):
    return seeded(key).format(**values)


def moves(bot):
    return [call for call in bot.backend.calls if call.endpoint.endswith("/move")]


def bot_state(bot) -> dict:
    return json.loads(bot.database.user.get("bot_state") or "{}")


def user_data(bot):
    return bot.application.user_data[DEFAULT_USER_ID]


def keyboard_rows(shown):
    """The reply keyboard's rows as (label, asks for the location)."""
    return [
        [(button["text"], bool(button.get("request_location"))) for button in row]
        for row in shown.reply_markup.get("keyboard") or []
    ]


LOCATION_KEYBOARD = [[(UZ["telegram.address.share_location_button"], True)], [(CANCEL_BUTTON, False)]]


def reply_keyboards(bot):
    """Every sendMessage that put a reply keyboard up, in order: the last one is what the customer has on screen."""
    return [call for call in bot.telegram.of("sendMessage") if "keyboard" in call.reply_markup]


async def ask_for_the_new_pin(bot, user, address_id=91):
    """Profile -> My addresses -> the address -> Edit -> Edit location."""
    await bot.send(user.tap(f"edit_address_{address_id}"))
    await bot.send(user.tap(f"edit_location_{address_id}"))


def age_the_prompt(bot, minutes):
    """The prompt was armed `minutes` ago: what the wall clock would do."""
    row = bot_state(bot)
    row["armed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    bot.database.user["bot_state"] = json.dumps(row)


async def test_edit_location_then_a_pin_moves_that_address_and_keeps_its_text(bot, user):
    await ask_for_the_new_pin(bot, user)

    prompt = bot.telegram.last_shown()
    assert prompt.text == seeded_text("telegram.address.location_edit_prompt", label="Uy")
    assert "parse_mode" not in prompt.params, "the label is the customer's own text, sent as written"
    assert keyboard_rows(prompt) == LOCATION_KEYBOARD
    armed = bot_state(bot)
    assert {k: armed[k] for k in ("awaiting_input", "relocate_address_id", "relocate_label")} == {
        "awaiting_input": "relocate_address", "relocate_address_id": 91, "relocate_label": "Uy",
    }
    assert datetime.now(timezone.utc) - datetime.fromisoformat(armed["armed_at"]) < timedelta(minutes=1)

    await bot.send(user.location(LAT, LNG))

    assert [(call.method, call.endpoint, call.data) for call in moves(bot)] == [
        ("POST", "/api/v1/auth/addresses/91/move", {"latitude": LAT, "longitude": LNG, "point_source": "map_pin"}),
    ]
    assert creates(bot) == [], "a moved address is not a new one"
    assert support_posts(bot) == []
    updated = bot.telegram.last_shown()
    assert updated.text == seeded_text("telegram.address.location_updated", label="Uy", line="Chilonzor 12-kv, 5-uy")
    assert "parse_mode" not in updated.params
    assert "keyboard" in updated.reply_markup and not any(
        asks for row in keyboard_rows(updated) for _label, asks in row
    ), "the menu keyboard replaces the location keyboard"
    assert (bot.backend.addresses[91]["latitude"], bot.backend.addresses[91]["longitude"]) == (LAT, LNG)
    assert bot.backend.addresses[91]["full_address"] == "Chilonzor 12-kv, 5-uy"
    assert "awaiting_input" not in bot_state(bot), "one pin answers the prompt"
    assert state(bot) is None and "temp_address_data" not in user_data(bot)


async def test_a_pin_outside_the_zone_asks_again_and_keeps_the_prompt(bot, user):
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.location(OUTSIDE_LAT, OUTSIDE_LNG))

    assert moves(bot) == []
    refused = bot.telegram.last_shown()
    assert refused.text == UZ["telegram.address.outside_delivery_area"]
    assert keyboard_rows(refused) == LOCATION_KEYBOARD
    assert bot_state(bot)["awaiting_input"] == "relocate_address"

    await bot.send(user.location(LAT, LNG))

    assert [call.endpoint for call in moves(bot)] == ["/api/v1/auth/addresses/91/move"]


async def test_add_address_after_edit_location_creates_a_new_address_and_never_moves(bot, user):
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.tap("add_new_address"))
    assert bot_state(bot).get("awaiting_input") is None, "the add-address pin prompt disarms Edit location"
    await bot.send(user.location(LAT, LNG))
    assert state(bot) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))

    assert moves(bot) == []
    assert [(call.data["latitude"], call.data["longitude"]) for call in creates(bot)] == [(LAT, LNG)]
    assert bot.backend.addresses[91] == UY


async def test_the_same_holds_from_checkout(bot, user):
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.tap("add_new_address_checkout"))
    await bot.send(user.location(LAT, LNG))
    assert state(bot) == ADDRESS_TITLE
    await bot.send(user.tap("addr_title_home"))

    assert moves(bot) == []
    assert [(call.data["latitude"], call.data["longitude"]) for call in creates(bot)] == [(LAT, LNG)]
    assert bot.backend.addresses[91] == UY


async def test_the_prompt_survives_a_restart_and_the_pin_moves_the_right_address(bot, user):
    await ask_for_the_new_pin(bot, user, address_id=92)
    restart(bot)

    await bot.send(user.location(LAT, LNG))

    assert [(call.endpoint, call.data) for call in moves(bot)] == [
        ("/api/v1/auth/addresses/92/move", {"latitude": LAT, "longitude": LNG, "point_source": "map_pin"}),
    ]
    assert creates(bot) == [] and support_posts(bot) == []
    assert bot.telegram.last_shown().text == seeded_text(
        "telegram.address.location_updated", label="Ish", line="Amir Temur 7, Mirobod",
    )
    assert bot.backend.addresses[91] == UY


async def test_a_stale_prompt_says_it_expired_and_neither_moves_nor_adds(bot, user):
    await ask_for_the_new_pin(bot, user)
    age_the_prompt(bot, AWAITING_LOCATION_STALE_MINUTES + 1)

    await bot.send(user.location(LAT, LNG))

    assert moves(bot) == [] and creates(bot) == [] and support_posts(bot) == []
    assert bot.telegram.last_shown().text == seeded("telegram.address.location_edit_expired")
    assert "awaiting_input" not in bot_state(bot)
    assert state(bot) is None and "temp_address_data" not in user_data(bot), "never Add address"


async def test_a_menu_tap_disarms_it_and_the_next_pin_goes_to_support(bot, user):
    await ask_for_the_new_pin(bot, user)
    bot.telegram.reset()

    await bot.send(user.text(SUPPORT_LABEL))
    assert "awaiting_input" not in bot_state(bot)
    [install] = menu_sends(bot)
    assert reply_keyboards(bot) == [install], "the flow exit takes the location keyboard down"

    await bot.send(user.location(LAT, LNG))

    assert moves(bot) == [] and creates(bot) == []
    assert [post["message_type"] for post in support_posts(bot)] == ["location"]


# The screens on the address-edit path that leave the prompt from an inline button, which is no flow exit: the edit
# menu above the prompt (its Back, Edit title, Edit instructions, the menu itself again), the address list and Profile.
# Each one takes the location keyboard down with the prompt, or its Share button would file the next pin to Support.
SCREENS_THAT_LEAVE_IT = [
    pytest.param("view_address_91", None, id="edit-menu-Back"),
    pytest.param("edit_address_91", None, id="edit-menu"),
    pytest.param("edit_title_91", "edit_address_title", id="Edit-title"),
    pytest.param("edit_instructions_91", "edit_address_instructions", id="Edit-instructions"),
    pytest.param("manage_addresses", None, id="My-addresses"),
    pytest.param("cancel_action", None, id="Profile"),
]


@pytest.mark.parametrize("tap,armed_after", SCREENS_THAT_LEAVE_IT)
async def test_an_address_screen_that_leaves_it_puts_the_menu_back(bot, user, tap, armed_after):
    await ask_for_the_new_pin(bot, user)
    let_the_dedup_window_lapse()  # `edit_address_91` was tapped once already
    bot.telegram.reset()

    await bot.send(user.tap(tap))

    assert bot_state(bot).get("awaiting_input") == armed_after
    [install] = menu_sends(bot)
    assert install.params["disable_notification"] is True
    assert reply_keyboards(bot) == [install], "the keyboard left on screen must be the bottom menu"


# Prompts armed over it from an inline button that is no flow exit and no address screen: an older Profile -> Edit
# profile menu (Name, Birthday) and a delivered order's "Report an issue". bot_state holds one prompt, so each one
# replaces Edit location's.
PROMPTS_ARMED_OVER_IT = [
    pytest.param("edit_profile_name", "edit_profile_name", id="Edit-profile-Name"),
    pytest.param("edit_profile_birthday", "edit_profile_birthday", id="Edit-profile-Birthday"),
    pytest.param("report_issue_7001", "support_message", id="Report-an-issue"),
]


@pytest.mark.parametrize("tap,armed_after", PROMPTS_ARMED_OVER_IT)
async def test_a_prompt_armed_over_it_takes_the_share_location_button_away(bot, user, tap, armed_after):
    """Left up, the Share-location button would send a pin that no longer moves anything: with Edit location's prompt
    gone from bot_state, the pin is filed to the Support Inbox in silence. So the bottom keyboard replaces it, and no
    Share-location button is left to send one."""
    await ask_for_the_new_pin(bot, user)
    bot.telegram.reset()

    await bot.send(user.tap(tap))

    assert bot_state(bot).get("awaiting_input") == armed_after
    [install] = menu_sends(bot)
    assert install.params["disable_notification"] is True
    assert reply_keyboards(bot) == [install], "the keyboard left on screen must be the bottom menu"
    assert support_posts(bot) == [] and moves(bot) == []


async def test_tapping_edit_location_again_sends_only_its_prompt(bot, user):
    """Its own location keyboard follows, so leaving the armed prompt sends no bottom keyboard in between."""
    await ask_for_the_new_pin(bot, user)
    let_the_dedup_window_lapse()
    bot.telegram.reset()

    await bot.send(user.tap("edit_location_91"))

    assert menu_sends(bot) == []
    [prompt] = reply_keyboards(bot)
    assert prompt.text == seeded_text("telegram.address.location_edit_prompt", label="Uy")
    assert keyboard_rows(prompt) == LOCATION_KEYBOARD
    assert bot_state(bot)["awaiting_input"] == "relocate_address"


async def test_the_keyboards_cancel_disarms_it(bot, user):
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.text(CANCEL_BUTTON))

    assert "awaiting_input" not in bot_state(bot)
    cancelled = bot.telegram.last_shown()
    assert cancelled.text == UZ["telegram.action_cancelled"]
    assert "keyboard" in cancelled.reply_markup, "the menu keyboard replaces the location keyboard"

    await bot.send(user.location(LAT, LNG))

    assert moves(bot) == [] and creates(bot) == []
    assert [post["message_type"] for post in support_posts(bot)] == ["location"]


async def test_the_edit_menu_offers_delete_not_delete_and_re_add(bot, user):
    await bot.send(user.tap("edit_address_91"))

    menu = bot.telegram.last_shown()
    labels = dict(zip(menu.callback_data(), menu.button_labels()))
    assert labels["delete_address_91"] == UZ["telegram.address.delete"]


# -- Phase 2: what the backend answers a move with (spec §6.6) -------------------------------------------------

MOVE = "/api/v1/auth/addresses/91/move"
REFUSAL_CODES = {
    "WITH_DRIVER": "ADDRESS_PIN_WITH_DRIVER",
    "SHARED_PLACE": "ADDRESS_PIN_SHARED_PLACE",
    "OUTLET": "ADDRESS_PIN_OUTLET",
    "TOO_FAR": "ADDRESS_MOVE_TOO_FAR",
}


def duplicate_409():
    """The move's 409 next to another saved address: no Move offer (this address is the one being moved)."""
    return backend_failure("Conflict", 409, data=possible_duplicate_body([ISH | {"distance_m": 31}]))


def guard_moves(bot):
    """The move refuses as a possible duplicate until its body confirms it with JSON true."""
    def respond(call):
        if (call.data or {}).get("confirm_duplicate") is True:
            return bot.backend._default(call)
        return duplicate_409()

    bot.backend.route("POST", MOVE, respond)


async def test_a_409_offers_save_here_anyway_and_that_tap_moves_with_the_flag(bot, user):
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.location(LAT, LNG))

    restored, prompt = bot.telegram.shown[-2:]
    assert restored.text == seeded("telegram.address.location_received")
    assert "keyboard" in restored.reply_markup and not any(
        asks for row in keyboard_rows(restored) for _label, asks in row
    ), "the menu keyboard replaces the location keyboard first"
    assert prompt.text == "\n".join([
        seeded("telegram.address.duplicate_prompt_header"),
        "   " + seeded_text("telegram.address.duplicate_prompt_line", label="Ish", distance=31),
        "   Amir Temur 7, Mirobod",
    ])
    assert "parse_mode" not in prompt.params
    assert prompt.callback_data() == [f"reloc_here_91_{LAT:.6f}_{LNG:.6f}", "reloc_cancel"]
    assert prompt.button_labels() == [
        seeded("telegram.address.duplicate_save_here_button"), UZ["telegram.cancel"],
    ]
    assert "awaiting_input" not in bot_state(bot), "the pin answered the prompt; the tap carries its point"
    assert [m.data for m in moves(bot)] == [
        {"latitude": LAT, "longitude": LNG, "point_source": "map_pin"},
    ]

    await bot.send(user.tap(f"reloc_here_91_{LAT:.6f}_{LNG:.6f}"))

    assert [m.data for m in moves(bot)][-1] == {
        "latitude": round(LAT, 6), "longitude": round(LNG, 6), "point_source": "map_pin", "confirm_duplicate": True,
    }
    assert bot.telegram.last_shown().text == seeded_text(
        "telegram.address.location_updated", label="Uy", line="Chilonzor 12-kv, 5-uy",
    )
    assert (bot.backend.addresses[91]["latitude"], bot.backend.addresses[91]["longitude"]) == (LAT, LNG)
    assert creates(bot) == [] and support_posts(bot) == []


async def test_cancel_at_the_409_prompt_moves_nothing(bot, user):
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    calls = len(moves(bot))

    await bot.send(user.tap("reloc_cancel"))

    assert bot.telegram.last_shown().text == UZ["telegram.action_cancelled"]
    assert len(moves(bot)) == calls
    assert bot.backend.addresses[91] == UY


async def test_save_here_survives_a_restart_because_its_point_rides_the_button(bot, user):
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    restart(bot)

    await bot.send(user.tap(f"reloc_here_91_{LAT:.6f}_{LNG:.6f}"))

    assert moves(bot)[-1].data["confirm_duplicate"] is True
    assert (bot.backend.addresses[91]["latitude"], bot.backend.addresses[91]["longitude"]) == (LAT, LNG)


SAVE_HERE = f"reloc_here_91_{LAT:.6f}_{LNG:.6f}"
# Telegram's answer to an edit that would leave the message as it is.
NOT_MODIFIED = (
    "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as "
    "a current content and reply markup of the message"
)


@pytest.mark.parametrize("tap", [SAVE_HERE, "reloc_cancel"], ids=["save-here-anyway", "cancel"])
async def test_a_second_tap_on_the_409_prompt_is_no_error(bot, user, tap, caplog):
    """The button stays on screen until the first answer lands, so a slow backend gets a second tap. It repeats the
    answer (a second move within the tolerance is a no-op), whose edit to the same text Telegram refuses as not
    modified: the customer has their answer already, so nothing more is said, least of all "something went wrong",
    and nothing is logged as a handler error."""
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    await bot.send(user.tap(tap))
    answer = bot.telegram.last_shown()
    assert answer.method == "editMessageText" and answer.refused is None
    bot.telegram.reset()
    bot.telegram.fail("editMessageText", NOT_MODIFIED)
    let_the_dedup_window_lapse()
    caplog.clear()

    await bot.send(user.tap(tap))

    assert [(c.method, c.text, c.refused) for c in bot.telegram.shown] == [
        ("editMessageText", answer.text, NOT_MODIFIED),
    ], "only the repeated answer, which Telegram refused as unchanged"
    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [None], "no error reply"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert bot.backend.addresses[91]["latitude"] == (LAT if tap == SAVE_HERE else UY["latitude"])


@pytest.mark.parametrize(
    ("move", "listed"),
    [
        (None, ()),
        (
            backend_failure(
                "Bad Request", 400,
                data={"error_code": "ADDRESS_PIN_SHARED_PLACE", "details": {"reason": "SHARED_PLACE"}},
            ),
            ("/api/v1/auth/addresses",),
        ),
    ],
    ids=["moved", "refused"],
)
async def test_save_here_anyway_answers_the_tap_before_any_backend_call(bot, user, move, listed):
    """The spinner stops at once: left up for the move (and, on a refusal, the list read for the label), it invites
    the second tap above."""
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    if move is not None:
        bot.backend.route("POST", MOVE, lambda _call: move)
    seen = acks_seen_by(bot, "POST", MOVE)
    seen_listed = acks_seen_by(bot, "GET", *listed)
    bot.telegram.reset()

    await bot.send(user.tap(SAVE_HERE))

    assert (seen, seen_listed) == ([1], [1] * len(listed)), "answers given when each backend call arrived"


def broken(_call):
    raise RuntimeError("backend connection reset")


def fail_the_next_send(bot):
    """Telegram answers the next sendMessage with a transient 502 Bad Gateway; the ones after it go through."""
    def once(_params):
        bot.telegram.failures.pop("sendMessage")
        return 502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}

    bot.telegram.failures["sendMessage"] = once


def the_error_reached_the_customer(bot, method):
    """The tap was answered quietly before the failure, and Telegram shows only the first answer to a tap: an error
    sent as a second answer reaches no one. So it is said where the customer reads it, as `method`."""
    assert [c.params.get("text") for c in bot.telegram.visible_answers] == [None], "the tap's one answer is quiet"
    assert [(c.method, c.text) for c in bot.telegram.delivered] == [(method, seeded("telegram.error_occurred"))]


async def test_an_unexpected_failure_after_save_here_anyway_is_said_on_the_prompt(bot, user):
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    bot.backend.route("POST", MOVE, broken)
    bot.telegram.reset()

    await bot.send(user.tap(SAVE_HERE))

    the_error_reached_the_customer(bot, "editMessageText")
    assert bot.backend.addresses[91] == UY


async def test_an_unexpected_failure_after_cancel_is_said_in_the_chat(bot, user):
    """The prompt is past Telegram's 48 h edit window, and the message that replaces it meets a transient 502."""
    guard_moves(bot)
    await ask_for_the_new_pin(bot, user)
    await bot.send(user.location(LAT, LNG))
    bot.telegram.reset()
    bot.telegram.fail("editMessageText", "Bad Request: message can't be edited")
    fail_the_next_send(bot)

    await bot.send(user.tap("reloc_cancel"))

    the_error_reached_the_customer(bot, "sendMessage")
    assert bot.backend.addresses[91] == UY


@pytest.mark.parametrize("refusal", [None, "WITH_DRIVER"], ids=["the-pin-prompt", "a-published-refusal"])
async def test_an_unexpected_failure_after_edit_location_answers_is_said_in_the_chat(bot, user, refusal):
    """The message that follows the tap's quiet answer (the pin prompt, or the refusal) meets a transient 502."""
    bot.backend.pin_move_refusals[91] = refusal
    await bot.send(user.tap("edit_address_91"))
    bot.telegram.reset()
    fail_the_next_send(bot)

    await bot.send(user.tap("edit_location_91"))

    the_error_reached_the_customer(bot, "sendMessage")
    assert moves(bot) == []


@pytest.mark.parametrize("address_id,lat,lng", [(1234567, -41.123456, -69.123456), (91, 41.32354, 69.241036)])
async def test_the_save_here_button_fits_telegrams_callback_limit(address_id, lat, lng):
    from keyboards import ProfileKeyboards

    markup = ProfileKeyboards.relocation_duplicate("uz", address_id, lat, lng)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == [f"reloc_here_{address_id}_{lat:.6f}_{lng:.6f}", "reloc_cancel"]
    assert all(len(d.encode()) <= 64 for d in data)


@pytest.mark.parametrize("reason", ["WITH_DRIVER", "SHARED_PLACE", "OUTLET", "TOO_FAR"])
async def test_each_pin_refusal_shows_its_own_copy(bot, user, reason):
    bot.backend.route(
        "POST", MOVE,
        lambda _call: backend_failure(
            "Bad Request", 400, data={"error_code": REFUSAL_CODES[reason], "details": {"reason": reason}},
        ),
    )
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.location(LAT, LNG))

    refusal = bot.telegram.last_shown()
    assert refusal.text == seeded(PIN_REFUSAL_COPY[reason]).format(label="Uy")
    assert "parse_mode" not in refusal.params
    assert "keyboard" in refusal.reply_markup and not any(
        asks for row in keyboard_rows(refusal) for _label, asks in row
    ), "the refusal comes with the menu"
    assert "awaiting_input" not in bot_state(bot)
    assert bot.backend.addresses[91] == UY


async def test_another_failure_says_something_went_wrong_with_the_menu(bot, user):
    bot.backend.route("POST", MOVE, lambda _call: backend_failure("boom", 500, data={}))
    await ask_for_the_new_pin(bot, user)

    await bot.send(user.location(LAT, LNG))

    failed = bot.telegram.last_shown()
    assert failed.text == seeded("telegram.error_occurred")
    assert "keyboard" in failed.reply_markup


def assert_the_refusal_is_a_message(bot, text):
    """The tap is answered quietly and the refusal comes as a message, sent as written (no parse mode, no keyboard):
    a tap's answer is a toast that is easy to miss, and Telegram refuses one longer than its limit."""
    assert [c.params.get("text") for c in bot.telegram.visible_answers] == [None], "a quiet answer to the tap"
    assert [(c.method, c.text, c.refused) for c in bot.telegram.shown] == [("sendMessage", text, None)]
    [message] = bot.telegram.shown
    assert "parse_mode" not in message.params and message.reply_markup == {}


@pytest.mark.parametrize("refusal", ["WITH_DRIVER", "SHARED_PLACE", "OUTLET"])
async def test_a_published_refusal_answers_before_any_pin_is_asked(bot, user, refusal):
    bot.backend.pin_move_refusals[91] = refusal
    await bot.send(user.tap("edit_address_91"))
    bot.telegram.reset()

    await bot.send(user.tap("edit_location_91"))

    assert reply_keyboards(bot) == [], "no location keyboard"
    assert "awaiting_input" not in bot_state(bot), "not armed"
    assert_the_refusal_is_a_message(bot, seeded_text(PIN_REFUSAL_COPY[refusal], label="Uy"))
    assert moves(bot) == []


# Telegram caps the text of a tap's answer at 200 characters, toast and popup alike, and refuses a longer one with
# MESSAGE_TOO_LONG: the spinner then stops with nothing shown.
ANSWER_TEXT_LIMIT = 200


def telegram_refuses_long_answers(bot):
    def answer(params):
        if len((params.get("text") or "").encode("utf-16-le")) // 2 > ANSWER_TEXT_LIMIT:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: MESSAGE_TOO_LONG"}
        return 200, {"ok": True, "result": True}

    bot.telegram.failures["answerCallbackQuery"] = answer


# An untitled address an admin created: its label is its whole written line.
LONG_LINE = "Chilonzor tumani, Bunyodkor shoh ko'chasi 15-uy, 3-podyezd, 4-qavat, 12-xonadon"


@pytest.mark.parametrize("refusal", ["SHARED_PLACE", "OUTLET"])
async def test_a_refusal_naming_a_long_label_still_reaches_the_customer(bot, user, refusal):
    """The uz Support explanation plus a 79-character label is past Telegram's limit for a tap's answer: sent as the
    answer, Telegram refuses it and the customer sees nothing, tap after tap."""
    untitled = address_row(91, full_address=LONG_LINE, latitude=41.3111, longitude=69.2797, is_default=True)
    bot.backend.addresses[91] = untitled
    bot.backend.pin_move_refusals[91] = refusal
    telegram_refuses_long_answers(bot)
    await bot.send(user.tap("edit_address_91"))
    bot.telegram.reset()

    await bot.send(user.tap("edit_location_91"))

    text = seeded_text(PIN_REFUSAL_COPY[refusal], label=untitled["display_label"])
    assert untitled["display_label"] == LONG_LINE and len(text) > ANSWER_TEXT_LIMIT, "the set-up names a long label"
    assert_the_refusal_is_a_message(bot, text)
    assert "awaiting_input" not in bot_state(bot) and moves(bot) == []


async def test_typed_text_while_armed_asks_for_the_pin_and_stays_armed(bot, user):
    await ask_for_the_new_pin(bot, user)
    armed_at = bot_state(bot)["armed_at"]

    await bot.send(user.text("Chilonzor 5"))

    assert bot.telegram.last_shown().text == seeded_text("telegram.address.location_edit_send_pin", label="Uy")
    assert bot_state(bot)["awaiting_input"] == "relocate_address"
    assert bot_state(bot)["armed_at"] == armed_at
    assert support_posts(bot) == [] and moves(bot) == []


async def test_typed_text_after_the_prompt_went_stale_says_it_expired(bot, user):
    await ask_for_the_new_pin(bot, user)
    age_the_prompt(bot, AWAITING_LOCATION_STALE_MINUTES + 1)

    await bot.send(user.text("Chilonzor 5"))

    assert bot.telegram.last_shown().text == seeded("telegram.address.location_edit_expired")
    assert "awaiting_input" not in bot_state(bot)
    assert support_posts(bot) == []
