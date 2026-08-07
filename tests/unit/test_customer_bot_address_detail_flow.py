"""The optional address-detail chain shared by both customer-bot address flows.

Both the geolocation flow (share a pin) and the manual flow (type a district and
street) converge on ``apartment -> floor -> delivery instructions``. Every step
is optional; a private house skips all three.

There is deliberately no entrance step. ``UserAddress`` has no ``entrance``
column, so the manual flow used to prompt for it and throw the answer away.
Entrance is now captured as free text by the delivery-instructions prompt.

Two environment landmines this file works around, both of which silently make
assertions meaningless rather than failing loudly:

* ``telegram_bot`` modules use workdir-relative BARE imports (``from i18n import
  i18n``), so they are NOT importable as ``telegram_bot.handlers.profile``; the
  package directory has to go on ``sys.path`` and the BARE module path is what
  ``monkeypatch`` must target.
* ``i18n.get`` does NOT fall back to the key. On a missing key it returns the
  humanised last segment and then ``.format()`` silently DROPS every kwarg — so
  in an unseeded test process an assertion on rendered copy would pass against
  broken code. The stub is mandatory.
"""

import asyncio
import pathlib
import re
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import handlers.profile as profile_mod  # noqa: E402
from handlers.profile import (  # noqa: E402
    ADDRESS_APARTMENT,
    ADDRESS_BUILDING,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_FLOOR,
    ADDRESS_TITLE,
    profile_handlers,
)


@pytest.fixture(autouse=True)
def _stub_i18n(monkeypatch):
    """Echo the key plus every interpolated value, so a wrong key or a dropped
    placeholder is visible in the rendered text."""
    monkeypatch.setattr(
        profile_mod.i18n,
        "get",
        lambda key, language=None, *a, **kw: " ".join([key] + [str(v) for v in kw.values()]),
    )
    monkeypatch.setattr(
        profile_mod.i18n, "get_user_language", AsyncMock(return_value="en")
    )


def _message_update(text=""):
    update = MagicMock()
    update.effective_user = MagicMock(id=555)
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _callback_update(data):
    update = MagicMock()
    update.effective_user = MagicMock(id=555)
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _context(**temp_address):
    context = MagicMock()
    context.user_data = {"temp_address_data": dict(temp_address)}
    return context


def _geo_context(**extra):
    """A context mid-way through the geolocation flow: pin already accepted."""
    return _context(
        latitude=41.311081,
        longitude=69.240562,
        location_source="shared",
        full_address="Amir Temur ko'chasi 1, Tashkent",
        **extra,
    )


def _manual_context(**extra):
    """A context mid-way through the manual flow: geocode confirmed.

    By the time the manual flow reaches the title step it already has
    latitude/longitude too — ``geocode_and_confirm`` always sets them. That is
    exactly why the title handlers must gate on ``location_source`` rather
    than on coordinate presence to tell the two flows apart.
    """
    return _context(
        latitude=41.311081,
        longitude=69.240562,
        location_source="manual",
        full_address="Amir Temur ko'chasi 1, Tashkent",
        **extra,
    )


def _sent_text(update):
    if update.callback_query is not None:
        call = update.callback_query.edit_message_text.call_args
    else:
        call = update.message.reply_text.call_args
    return call.args[0] if call.args else call.kwargs["text"]


# ---------------------------------------------------------------------------
# Task 1 — the manual flow converges; entrance is gone
# ---------------------------------------------------------------------------


def test_a_floor_answer_leads_to_delivery_instructions_not_entrance():
    update = _message_update("3")
    context = _context(street_address="Amir Temur", building_number="12")

    state = asyncio.run(profile_handlers.floor_received(update, context))

    assert state == ADDRESS_DELIVERY_INSTRUCTIONS
    assert context.user_data["temp_address_data"]["floor_number"] == "3"
    assert "telegram.address.enter_delivery_instructions" in _sent_text(update)


def test_skipping_floor_leads_to_delivery_instructions():
    update = _callback_update("skip_floor")

    state = asyncio.run(profile_handlers.skip_field_handler(update, _context()))

    assert state == ADDRESS_DELIVERY_INSTRUCTIONS
    assert "telegram.address.enter_delivery_instructions" in _sent_text(update)


def test_an_apartment_answer_still_leads_to_floor():
    update = _message_update("42")
    context = _context()

    state = asyncio.run(profile_handlers.apartment_received(update, context))

    assert state == ADDRESS_FLOOR
    assert context.user_data["temp_address_data"]["apartment_number"] == "42"
    assert "telegram.address.enter_floor" in _sent_text(update)


def test_skipping_the_building_still_jumps_past_apartment_and_floor():
    # No building number means there is no building to be inside — a private
    # house should not then be asked which apartment and floor it is on.
    update = _callback_update("skip_building")

    state = asyncio.run(profile_handlers.skip_field_handler(update, _context()))

    assert state == ADDRESS_DELIVERY_INSTRUCTIONS


def test_a_street_answer_leads_to_the_building_step():
    update = _message_update("Amir Temur")
    context = _context()

    state = asyncio.run(profile_handlers.street_received(update, context))

    assert state == ADDRESS_BUILDING
    assert context.user_data["temp_address_data"]["street_address"] == "Amir Temur"


def test_the_entrance_step_no_longer_exists():
    assert not hasattr(profile_mod, "ADDRESS_ENTRANCE")
    assert not hasattr(profile_handlers, "entrance_received")


def test_a_stale_skip_entrance_tap_ends_the_conversation_instead_of_crashing():
    # An inline keyboard rendered before this deploy can still be tapped after it.
    update = _callback_update("skip_entrance")

    state = asyncio.run(profile_handlers.skip_field_handler(update, _context()))

    assert state == profile_mod.ConversationHandler.END


def test_bot_py_registers_no_entrance_state():
    # Read as source rather than importing: `import bot` runs setup_logging().
    bot_source = (REPO_ROOT / "telegram_bot" / "bot.py").read_text()
    assert "ADDRESS_ENTRANCE" not in bot_source


# ---------------------------------------------------------------------------
# Task 2 — the geolocation flow joins the chain
# ---------------------------------------------------------------------------


class _AsyncClient:
    """Async-context-manager stand-in for the module-level ``api_client``."""

    def __init__(self, add_user_address):
        self.client = MagicMock()
        self.client.add_user_address = add_user_address

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def captured_save(monkeypatch):
    """Capture the payload handed to POST /addresses."""
    add_user_address = AsyncMock(return_value=MagicMock(success=True, data={}))
    monkeypatch.setattr(profile_mod, "api_client", _AsyncClient(add_user_address))
    monkeypatch.setattr(profile_mod, "get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(profile_mod, "main_menu_for", AsyncMock(return_value=MagicMock()))
    return add_user_address


def _saved_payload(captured_save):
    return captured_save.await_args.args[1]


def test_a_typed_title_leads_into_the_detail_chain():
    update = _message_update("Mum's place")
    context = _geo_context()

    state = asyncio.run(profile_handlers.address_title_received(update, context))

    assert state == ADDRESS_APARTMENT
    assert context.user_data["temp_address_data"]["title"] == "Mum's place"
    assert "telegram.address.enter_apartment" in _sent_text(update)


@pytest.mark.parametrize(
    "callback_data", ["addr_title_home", "addr_title_work", "addr_title_other"]
)
def test_a_title_suggestion_asks_for_apartment_instead_of_saving(monkeypatch, callback_data):
    # The regression this locks: tapping Home used to call save_address_final
    # immediately, so it never reached delivery instructions either.
    saved = AsyncMock()
    monkeypatch.setattr(profile_handlers, "save_address_final", saved)

    update = _callback_update(callback_data)
    context = _geo_context()

    state = asyncio.run(profile_handlers.address_title_callback(update, context))

    assert state == ADDRESS_APARTMENT
    saved.assert_not_awaited()
    assert "telegram.address.enter_apartment" in _sent_text(update)


def test_a_private_house_skips_every_detail_and_saves_without_them(captured_save):
    context = _geo_context(title="Home")

    assert asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_apartment"), context)
    ) == ADDRESS_FLOOR
    assert asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_floor"), context)
    ) == ADDRESS_DELIVERY_INSTRUCTIONS
    asyncio.run(
        profile_handlers.skip_field_handler(
            _callback_update("skip_delivery_instructions"), context
        )
    )

    payload = _saved_payload(captured_save)
    assert payload["title"] == "Home"
    assert "apartment_number" not in payload
    assert "floor_number" not in payload


def test_entered_details_reach_the_save_payload(captured_save):
    context = _geo_context(title="Home")

    asyncio.run(profile_handlers.apartment_received(_message_update("42"), context))
    asyncio.run(profile_handlers.floor_received(_message_update("7"), context))
    asyncio.run(
        profile_handlers.delivery_instructions_received(
            _message_update("2nd entrance, gate code 1234"), context
        )
    )

    payload = _saved_payload(captured_save)
    assert payload["apartment_number"] == "42"
    assert payload["floor_number"] == "7"
    # Entrance has no column; it survives inside the free-text instructions.
    assert payload["delivery_instructions"] == "2nd entrance, gate code 1234"


# ---------------------------------------------------------------------------
# Fix round 1 — the manual flow must not loop back through the detail chain
# ---------------------------------------------------------------------------
#
# The title step sits at a DIFFERENT position in each flow: the geolocation
# flow asks for it early (right after the pin, with apartment/floor/
# instructions still ahead), while the manual flow only reaches it LAST,
# after geocode confirmation — where it used to be the flow's sole exit.
# Routing every title answer into "go to apartment" (this task's original
# fix) broke that exit: the manual flow would loop
# title -> apartment -> floor -> instructions -> geocode -> confirm -> title -> ...
# forever. The geo-flow tests above all use ``_geo_context`` (location_source
# == "shared") so they never exercised this path.


def test_a_typed_title_in_the_manual_flow_saves_instead_of_looping(captured_save):
    context = _manual_context()
    update = _message_update("Office")

    state = asyncio.run(profile_handlers.address_title_received(update, context))

    assert state == profile_mod.ConversationHandler.END
    payload = _saved_payload(captured_save)
    assert payload["title"] == "Office"


def test_a_title_suggestion_in_the_manual_flow_saves_instead_of_looping(captured_save):
    context = _manual_context()
    update = _callback_update("addr_title_home")

    state = asyncio.run(profile_handlers.address_title_callback(update, context))

    assert state == profile_mod.ConversationHandler.END
    payload = _saved_payload(captured_save)
    assert payload["title"] == "Home"


def test_manual_flow_through_confirm_geocode_and_title_terminates_without_looping(
    captured_save,
):
    # This is the actual loop regression: drive the manual flow from geocode
    # confirmation through the title step and assert it ends the
    # conversation rather than re-entering the apartment/floor/instructions
    # chain (which would send it straight back to geocode_and_confirm).
    context = _manual_context()

    confirm_state = asyncio.run(
        profile_handlers.confirm_geocode(_callback_update("confirm_geocode"), context)
    )
    assert confirm_state == ADDRESS_TITLE

    state = asyncio.run(
        profile_handlers.address_title_received(_message_update("Office"), context)
    )

    assert state == profile_mod.ConversationHandler.END


# ---------------------------------------------------------------------------
# Task 3 — length guard
# ---------------------------------------------------------------------------


def test_the_cap_matches_the_database_column_width():
    # If someone widens the column, this fails instead of the bot quietly
    # rejecting values the database would now accept.
    from business_app.models.user import UserAddress

    columns = UserAddress.__table__.c
    assert profile_mod.ADDRESS_DETAIL_MAX_LENGTH == columns.apartment_number.type.length
    assert profile_mod.ADDRESS_DETAIL_MAX_LENGTH == columns.floor_number.type.length


# Both length-capped fields, exercised on BOTH sides of the boundary. Written
# as one table so a third capped field cannot be added with only one side
# covered — which is how `floor` ended up with no exact-limit test.
#   (step name, handler, temp_address_data key, state a valid answer leads to)
_CAPPED_DETAIL_FIELDS = [
    ("apartment", "apartment_received", "apartment_number", ADDRESS_FLOOR),
    ("floor", "floor_received", "floor_number", ADDRESS_DELIVERY_INSTRUCTIONS),
]


@pytest.mark.parametrize("field, handler, data_key, next_state", _CAPPED_DETAIL_FIELDS)
def test_a_detail_exactly_at_the_limit_is_accepted(field, handler, data_key, next_state):
    update = _message_update("x" * 20)
    context = _geo_context()

    state = asyncio.run(getattr(profile_handlers, handler)(update, context))

    assert state == next_state
    assert context.user_data["temp_address_data"][data_key] == "x" * 20


@pytest.mark.parametrize("field, handler, data_key, next_state", _CAPPED_DETAIL_FIELDS)
def test_an_overlong_detail_reprompts_instead_of_advancing(
    field, handler, data_key, next_state
):
    update = _message_update("x" * 21)
    context = _geo_context()

    state = asyncio.run(getattr(profile_handlers, handler)(update, context))

    # Re-prompts its OWN step rather than advancing.
    assert state == profile_mod._ADDRESS_STEPS[field].state
    assert data_key not in context.user_data["temp_address_data"]
    assert "telegram.address.field_too_long" in _sent_text(update)


def test_the_too_long_message_tells_the_customer_the_limit():
    update = _message_update("x" * 21)

    asyncio.run(profile_handlers.apartment_received(update, _geo_context()))

    # The i18n stub echoes interpolated values, so a dropped placeholder shows up.
    assert "20" in _sent_text(update)


# ---------------------------------------------------------------------------
# Task 4 — dead code
# ---------------------------------------------------------------------------


def test_the_unregistered_text_entry_handler_is_gone():
    # It was never wired into any ConversationHandler state, and the only reader
    # of the `temp_address` key it wrote was removed in Task 2.
    assert not hasattr(profile_handlers, "address_text_received")


def test_nothing_still_reads_the_orphaned_temp_address_key():
    source = (REPO_ROOT / "telegram_bot" / "handlers" / "profile.py").read_text()
    # Only the defensive pops in the cancel/save paths may mention it.
    reads = [
        line.strip()
        for line in source.splitlines()
        if "temp_address'" in line and ".pop(" not in line
    ]
    assert reads == []


# ---------------------------------------------------------------------------
# Task 5 — copy
# ---------------------------------------------------------------------------

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

LANGUAGES = ("en", "uz", "ru")


def test_the_instructions_prompt_asks_for_entrance_in_every_language():
    # This prompt is the ONLY place entrance is captured — there is no entrance
    # step and no entrance column. If the hint drops it, the feature is gone.
    copy = BACKEND_TRANSLATIONS["telegram.address.enter_delivery_instructions"]
    expected_entrance_word = {"en": "entrance", "uz": "podyezd", "ru": "подъезд"}

    for language in LANGUAGES:
        assert expected_entrance_word[language] in copy[language].lower()


def test_the_instructions_prompt_asks_for_a_gate_or_intercom_code():
    copy = BACKEND_TRANSLATIONS["telegram.address.enter_delivery_instructions"]
    expected_code_word = {"en": "code", "uz": "kod", "ru": "код"}

    for language in LANGUAGES:
        assert expected_code_word[language] in copy[language].lower()


def test_the_too_long_message_is_seeded_with_its_placeholder():
    copy = BACKEND_TRANSLATIONS["telegram.address.field_too_long"]

    for language in LANGUAGES:
        assert "{max_length}" in copy[language]


def test_the_retired_entrance_prompt_is_left_in_place():
    # Its DB rows stay; deleting seeded translation data buys nothing and is not
    # reversible from code. No handler reads it any more.
    assert "telegram.address.enter_entrance" in BACKEND_TRANSLATIONS


# ---------------------------------------------------------------------------
# Fix round 2 — Skip clears the value it skips
# ---------------------------------------------------------------------------
#
# Reachable through retry_geocode: type apartment 42 -> geocode -> "wrong
# location" -> share a pin instead -> the chain reruns from the top -> tap
# Skip on apartment. Navigating without touching temp_address_data left 42
# stored, and it was saved anyway — so Skip did not mean skip.


def test_skipping_apartment_clears_a_value_entered_before_a_retry_rerun():
    context = _geo_context(apartment_number="42")

    state = asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_apartment"), context)
    )

    assert state == ADDRESS_FLOOR
    assert "apartment_number" not in context.user_data["temp_address_data"]


def test_skipping_floor_clears_a_value_entered_before_a_retry_rerun():
    context = _geo_context(floor_number="7")

    state = asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_floor"), context)
    )

    assert state == ADDRESS_DELIVERY_INSTRUCTIONS
    assert "floor_number" not in context.user_data["temp_address_data"]


def test_skipping_the_building_clears_the_building_it_skips():
    context = _geo_context(building_number="12")

    state = asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_building"), context)
    )

    assert state == ADDRESS_DELIVERY_INSTRUCTIONS
    assert "building_number" not in context.user_data["temp_address_data"]


def test_an_unknown_skip_touches_nothing_before_ending():
    # A stale `skip_entrance` keyboard tapped after this deploy must not clear
    # somebody's real answers on its way out.
    context = _geo_context(apartment_number="42", floor_number="7")

    state = asyncio.run(
        profile_handlers.skip_field_handler(_callback_update("skip_entrance"), context)
    )

    assert state == profile_mod.ConversationHandler.END
    assert context.user_data["temp_address_data"]["apartment_number"] == "42"
    assert context.user_data["temp_address_data"]["floor_number"] == "7"


def test_skipping_instructions_keeps_a_previously_typed_note_out_of_the_payload(
    captured_save,
):
    context = _geo_context(title="Home", delivery_instructions="gate code 1234")

    asyncio.run(
        profile_handlers.skip_field_handler(
            _callback_update("skip_delivery_instructions"), context
        )
    )

    assert "delivery_instructions" not in _saved_payload(captured_save)


def test_every_skippable_step_knows_which_key_it_writes():
    # The stored key names differ from the step names (apartment ->
    # apartment_number), so the mapping is data, not an if-chain — and every
    # skippable field must appear in it or its Skip silently keeps the value.
    for field in profile_mod._SKIP_TARGETS:
        assert field in profile_mod._ADDRESS_FIELD_DATA_KEYS
    for field in profile_mod._ADDRESS_STEPS:
        assert field in profile_mod._ADDRESS_FIELD_DATA_KEYS


# ---------------------------------------------------------------------------
# Fix round 2 — the _ADDRESS_STEPS <-> bot.py registration invariant
# ---------------------------------------------------------------------------
#
# A step's Skip button is only live if bot.py registers `^skip_{field}$` under
# the SAME state the step returns. Drift in either direction strands a customer
# on a dead Skip button with no way forward but /cancel.

# `ADDRESS_DETAIL_MAX_LENGTH` shares the prefix but is a column width, not a
# conversation state, so it is excluded by name rather than by value.
_ADDRESS_STATE_NAMES = {
    value: name
    for name, value in vars(profile_mod).items()
    if name.startswith("ADDRESS_")
    and name != "ADDRESS_DETAIL_MAX_LENGTH"
    and isinstance(value, int)
}


def _bot_address_state_blocks():
    """{state constant name: source of its handler list} from telegram_bot/bot.py.

    Read as source rather than imported: `import bot` runs setup_logging().
    """
    source = (REPO_ROOT / "telegram_bot" / "bot.py").read_text()
    blocks, current, collected = {}, None, []
    for line in source.splitlines():
        stripped = line.strip()
        if current is None:
            match = re.fullmatch(r"(ADDRESS_[A-Z_]+): \[", stripped)
            if match:
                current, collected = match.group(1), []
            continue
        if stripped in ("]", "],"):
            blocks[current] = "\n".join(collected)
            current = None
            continue
        collected.append(stripped)
    return blocks


def test_the_bot_registers_a_state_block_for_every_step():
    blocks = _bot_address_state_blocks()
    for field, step in profile_mod._ADDRESS_STEPS.items():
        assert _ADDRESS_STATE_NAMES[step.state] in blocks, field


@pytest.mark.parametrize("field", sorted(profile_mod._ADDRESS_STEPS))
def test_each_step_state_registers_that_steps_skip_callback(field):
    step = profile_mod._ADDRESS_STEPS[field]
    block = _bot_address_state_blocks()[_ADDRESS_STATE_NAMES[step.state]]

    assert f'pattern="^skip_{field}$"' in block


@pytest.mark.parametrize("field", sorted(profile_mod._ADDRESS_STEPS))
def test_each_step_keyboard_emits_the_callback_its_state_listens_for(field):
    step = profile_mod._ADDRESS_STEPS[field]

    callbacks = [
        button.callback_data
        for row in step.keyboard("en").inline_keyboard
        for button in row
    ]

    assert f"skip_{field}" in callbacks


def test_the_step_table_self_documents_its_three_members():
    # A NamedTuple, so `step.state` at a call site cannot silently become
    # `step.prompt_key` when the table gains or reorders a member.
    step = profile_mod._ADDRESS_STEPS["apartment"]

    assert step._fields == ("prompt_key", "keyboard", "state")
    assert step.prompt_key == "telegram.address.enter_apartment"
    assert step.state == ADDRESS_APARTMENT
