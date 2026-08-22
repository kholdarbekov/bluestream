"""A shared map pin must never be thrown away because the customer stopped
answering OPTIONAL questions.

Production evidence (Loki, 30 days to 2026-08-21): 33 pin flows reached the
title step; only 13 ever wrote an address. The other 20 evaporated. One traced
session, telegram user 1009661971 on 2026-08-19:

    11:35:33  location_received       lat=41.32354 lng=69.241036  -> ADDRESS_TITLE
    11:35:46  address_title_callback  title "Ish"                 -> ADDRESS_APARTMENT
    11:35:52  skip_field_handler      skipped: apartment          -> ADDRESS_FLOOR
    11:36:02  skip_field_handler      skipped: floor              -> ADDRESS_DELIVERY_INSTRUCTIONS
              ... nothing. no save. no error. no address.

The bot held a complete, valid address (title + coordinates + reverse-geocoded
full_address) from 11:35:46 onwards and kept it in volatile `user_data` until
the customer answered three more questions that the UI itself renders a Skip
button for.

These tests drive the pin JOURNEY rather than one handler, because the defect
lives in the seam between the handlers, not inside any of them — which is
exactly why a suite full of green single-handler tests shipped it.
"""

from unittest.mock import AsyncMock

import pytest

# Imported at module (collection) level so `i18n`, `keyboards` and `config` are
# cached in sys.modules as the BOT's versions before any string-path
# monkeypatch below resolves them. See tests/telegram_bot/conftest.py.
from handlers.profile import (
    ADDRESS_APARTMENT,
    ADDRESS_BUILDING,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_FLOOR,
    ADDRESS_TITLE,
    ProfileHandlers,
)
from telegram.ext import ConversationHandler

from tests.telegram_bot.helpers import (
    DummyCallbackQuery,
    DummyLocation,
    DummyUpdate,
    make_context,
)

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


# Chilanzar; inside TASHKENT_POLYGON, so the real delivery-zone SSOT accepts it.
PIN_LAT = 41.2876
PIN_LNG = 69.2224
GEOCODED = "15, Chilonzor dahasi, Chilonzor Tumani, Toshkent shahri"


class _Response:
    """Mirrors api_client.APIResponse closely enough for these handlers."""

    def __init__(self, success=True, data=None, error=None, status_code=200):
        self.success = success
        self.data = data
        self.error = error
        self.status_code = status_code


class RecordingAddressAPI:
    """Stands in for the ONE thing these handlers legitimately cannot do in a
    test: HTTP to the backend.

    Records payloads rather than call counts so the assertions below can say
    what was written, not merely that something was.
    """

    def __init__(self, *, create_succeeds=True):
        self.created: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self.deleted: list[int] = []
        self.create_succeeds = create_succeeds
        self._next_id = 501

    async def reverse_geocode(self, _token, _lat, _lng):
        return _Response(data={"data": {"formatted_address": GEOCODED}})

    async def add_user_address(self, _token, payload):
        if not self.create_succeeds:
            return _Response(success=False, error="backend down", status_code=503)
        address_id = self._next_id
        self._next_id += 1
        self.created.append(dict(payload))
        return _Response(
            data={"data": {"address": {"id": address_id, **payload}}},
            status_code=201,
        )

    async def update_user_address(self, _token, address_id, payload):
        self.updated.append((address_id, dict(payload)))
        return _Response(data={"data": {"address": {"id": address_id, **payload}}})

    async def delete_user_address(self, _token, address_id):
        self.deleted.append(address_id)
        return _Response(data={"data": {}})

    async def get_user_addresses(self, _token):
        return _Response(data={"data": {"addresses": []}})


@pytest.fixture
def echo_i18n(monkeypatch):
    """i18n.get silently drops kwargs on a missing key, so an unstubbed render
    test passes against broken code. Echo the key plus every interpolated
    value instead."""

    def _get(key, language=None, *args, **kwargs):
        if kwargs:
            return f"{key}|" + "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    monkeypatch.setattr("i18n.i18n.get", _get)
    monkeypatch.setattr("i18n.i18n.get_user_language", AsyncMock(return_value="uz"))
    return _get


@pytest.fixture
def api(monkeypatch, echo_i18n):
    """Wire the recording API into handlers.profile and neuter the two other
    pieces of real I/O the flow reaches for (auth token, menu keyboard)."""
    recorder = RecordingAddressAPI()

    class _Client:
        async def __aenter__(self):
            return recorder

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("handlers.profile.api_client", _Client())
    monkeypatch.setattr("handlers.profile.get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("handlers.profile.main_menu_for", AsyncMock(return_value=None))
    return recorder


@pytest.fixture
def handler():
    return ProfileHandlers()


def pin_update(lat=PIN_LAT, lng=PIN_LNG):
    update = DummyUpdate()
    update.message.location = DummyLocation(lat, lng)
    return update


def tap(data):
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=data)
    return update


def typed(text):
    update = DummyUpdate()
    update.message.text = text
    return update


async def share_pin_and_name_it(handler, ctx, *, title_data="addr_title_home"):
    """The two steps after which the bot holds a complete, valid address."""
    assert await handler.location_received(pin_update(), ctx) == ADDRESS_TITLE
    return await handler.address_title_callback(tap(title_data), ctx)


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


async def test_pin_then_skipped_apartment_and_floor_then_abandon_still_leaves_an_address(
    handler, api
):
    """The traced production journey, end to end, stopping exactly where the
    customer stopped: after skipping floor, at the delivery-instructions
    prompt. An address MUST exist by then."""
    ctx = make_context()

    await share_pin_and_name_it(handler, ctx)
    assert await handler.skip_field_handler(tap("skip_apartment"), ctx) == ADDRESS_FLOOR
    assert (
        await handler.skip_field_handler(tap("skip_floor"), ctx)
        == ADDRESS_DELIVERY_INSTRUCTIONS
    )

    # The customer walks away here. Nothing else will ever arrive.
    assert len(api.created) == 1, (
        "the pin, the title and the reverse-geocoded address were all in hand "
        "before the first optional question; abandoning an OPTIONAL step must "
        "not discard them"
    )


async def test_the_address_created_at_the_title_step_carries_the_pin_and_the_title(
    handler, api
):
    """Creating early is only useful if the row is actually deliverable."""
    ctx = make_context()

    await share_pin_and_name_it(handler, ctx)

    assert len(api.created) == 1
    payload = api.created[0]
    assert payload["latitude"] == PIN_LAT
    assert payload["longitude"] == PIN_LNG
    assert payload["full_address"] == GEOCODED
    assert payload["title"] == "Uy", "uz title suggestion for addr_title_home"


async def test_a_typed_title_creates_the_address_too(handler, api):
    """Half the customers type a name instead of tapping a suggestion; the two
    paths must not disagree about when an address becomes real."""
    ctx = make_context()

    assert await handler.location_received(pin_update(), ctx) == ADDRESS_TITLE
    assert await handler.address_title_received(typed("Dacha"), ctx) == ADDRESS_APARTMENT

    assert [p["title"] for p in api.created] == ["Dacha"]


# ---------------------------------------------------------------------------
# Enrichment: the optional chain edits the row it already created
# ---------------------------------------------------------------------------


async def test_a_typed_apartment_is_pushed_onto_the_already_saved_address(handler, api):
    """An answer the customer gave must survive them abandoning the NEXT step,
    so it has to reach the backend when it is given, not at the end."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    assert await handler.apartment_received(typed("45"), ctx) == ADDRESS_FLOOR

    assert api.updated, "the apartment number never reached the backend"
    address_id, payload = api.updated[-1]
    assert address_id == 501
    assert payload["apartment_number"] == "45"


async def test_walking_the_whole_chain_creates_exactly_one_address(handler, api):
    """Creating early must not mean creating twice: the terminal step now
    finishes a row that already exists."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    await handler.apartment_received(typed("45"), ctx)
    await handler.floor_received(typed("9"), ctx)
    assert (
        await handler.delivery_instructions_received(typed("domofon 45"), ctx)
        == ConversationHandler.END
    )

    assert len(api.created) == 1, f"duplicate address rows: {api.created}"
    final_id, final_payload = api.updated[-1]
    assert final_id == 501
    assert final_payload["apartment_number"] == "45"
    assert final_payload["floor_number"] == "9"
    assert final_payload["delivery_instructions"] == "domofon 45"


async def test_skipping_every_optional_step_still_ends_with_one_complete_address(
    handler, api
):
    """The all-Skip journey — the most common completed shape in production."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    await handler.skip_field_handler(tap("skip_apartment"), ctx)
    await handler.skip_field_handler(tap("skip_floor"), ctx)
    assert (
        await handler.skip_field_handler(tap("skip_delivery_instructions"), ctx)
        == ConversationHandler.END
    )

    assert len(api.created) == 1
    assert api.deleted == [], "a completed flow must not delete its own address"


# ---------------------------------------------------------------------------
# Cancel is the one exit that means "I don't want this address"
# ---------------------------------------------------------------------------


async def test_cancelling_after_the_title_deletes_the_address_it_created(handler, api):
    """Saving early is only safe if an explicit Cancel still undoes it —
    otherwise the customer is left with a row they cancelled."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)
    assert len(api.created) == 1

    assert (
        await handler.cancel_address(tap("cancel_address_creation"), ctx)
        == ConversationHandler.END
    )

    assert api.deleted == [501]
    assert "temp_address_data" not in ctx.user_data


async def test_cancelling_from_the_text_button_deletes_the_address_too(handler, api):
    """The reply-keyboard Cancel is a second door out of the same flow."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    assert (
        await handler.cancel_address_text(typed("Cancel"), ctx) == ConversationHandler.END
    )

    assert api.deleted == [501]


# ---------------------------------------------------------------------------
# Guards on the blast radius
# ---------------------------------------------------------------------------


async def test_manual_entry_still_saves_only_after_geocode_confirmation(handler, api):
    """A typed address has no coordinates until it is geocoded and the customer
    confirms the pin, so it must NOT be created early. Only the shared-pin flow
    changes."""
    ctx = make_context()
    ctx.user_data["temp_address_data"] = {
        "district": "chilonzor",
        "district_name": "Chilanzar",
        "street_address": "Bunyodkor",
    }

    assert await handler.street_received(typed("Bunyodkor"), ctx) == ADDRESS_BUILDING
    assert await handler.building_received(typed("15"), ctx) == ADDRESS_APARTMENT
    await handler.apartment_received(typed("45"), ctx)
    await handler.floor_received(typed("9"), ctx)

    assert api.created == [], "manual entry has no confirmed coordinates yet"
    assert api.updated == [], "nothing exists to enrich yet"


async def test_a_failed_create_at_the_title_step_still_saves_at_the_end(
    handler, monkeypatch, echo_i18n
):
    """A transient backend failure must degrade to the old behaviour, not strand
    the customer in a chain that can never commit."""
    recorder = RecordingAddressAPI(create_succeeds=False)

    class _Client:
        async def __aenter__(self):
            return recorder

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("handlers.profile.api_client", _Client())
    monkeypatch.setattr("handlers.profile.get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("handlers.profile.main_menu_for", AsyncMock(return_value=None))

    ctx = make_context()
    assert await handler.location_received(pin_update(), ctx) == ADDRESS_TITLE
    assert (
        await handler.address_title_callback(tap("addr_title_home"), ctx)
        == ADDRESS_APARTMENT
    ), "a failed create must not end the conversation"

    recorder.create_succeeds = True
    await handler.skip_field_handler(tap("skip_apartment"), ctx)
    await handler.skip_field_handler(tap("skip_floor"), ctx)
    await handler.skip_field_handler(tap("skip_delivery_instructions"), ctx)

    assert len(recorder.created) == 1, "the terminal step must retry the create"


# ---------------------------------------------------------------------------
# The flow must never end silently
# ---------------------------------------------------------------------------


async def test_the_address_conversation_registers_a_timeout_state():
    """`conversation_timeout=600` with no TIMEOUT handler ends the flow with no
    message and leaves temp_address_data / address_flow_origin stranded in
    user_data, where a stale 'checkout' origin can hijack a later save.

    Reads the built Application rather than bot.py's TEXT: a source-substring
    check is satisfied by `ConversationHandler.TIMEOUT: []`, which registers the
    key and still expires in total silence.
    """
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler

    from bot import WaterBusinessBot

    application = ApplicationBuilder().token("424242:TEST-TOKEN").build()
    water_bot = WaterBusinessBot()
    water_bot.application = application
    await water_bot._setup_handlers()

    address = next(
        handler
        for group in application.handlers.values()
        for handler in group
        if isinstance(handler, ConversationHandler)
        and handler.name == "address_conversation"
    )

    assert address.conversation_timeout, "the flow is supposed to expire"
    timeout_handlers = address.states.get(ConversationHandler.TIMEOUT) or []
    assert timeout_handlers, (
        "the address conversation expires with no TIMEOUT handler, so it ends "
        "in total silence"
    )
    kinds = {type(handler) for handler in timeout_handlers}
    assert MessageHandler in kinds and CallbackQueryHandler in kinds, (
        "the timeout can fire while the customer is parked on either a typed "
        f"step or an inline one; both must be covered, got {kinds}"
    )


async def test_the_timeout_handler_clears_the_flow_state(handler, api):
    """Stale keys outlive the conversation that owned them: a leftover
    address_flow_origin='checkout' bounces a LATER, unrelated address save
    into checkout."""
    ctx = make_context()
    ctx.user_data["address_flow_origin"] = "checkout"
    await share_pin_and_name_it(handler, ctx)

    update = DummyUpdate()
    assert await handler.address_flow_timeout(update, ctx) == ConversationHandler.END

    assert "temp_address_data" not in ctx.user_data
    assert "address_flow_origin" not in ctx.user_data
    assert update.message.reply_text.await_count == 1, (
        "the customer must be told the flow expired"
    )


async def test_the_timeout_keeps_an_address_that_was_already_created(handler, api):
    """Timing out is not cancelling. The row the customer already earned by
    dropping a pin and naming it stays."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    await handler.address_flow_timeout(DummyUpdate(), ctx)

    assert api.deleted == [], "a timeout must not delete the saved address"


# ---------------------------------------------------------------------------
# Correcting the pin is not adding an address
# ---------------------------------------------------------------------------


async def test_re_sharing_a_corrected_pin_moves_the_address_instead_of_duplicating_it(
    handler, api
):
    """`location_received` is an ENTRY POINT and the conversation sets
    allow_reentry=True, so PTB re-enters on a second pin even mid-flow. A
    customer who notices the pin was wrong and drops a better one is CORRECTING
    the address they just named, not adding a second one — and the first pin's
    row must not be left behind at the wrong coordinates for a driver to
    deliver to.
    """
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)
    assert len(api.created) == 1

    corrected = DummyUpdate()
    corrected.message.location = DummyLocation(41.3106, 69.2401)
    assert await handler.location_received(corrected, ctx) == ADDRESS_TITLE
    await handler.address_title_callback(tap("addr_title_home"), ctx)

    assert len(api.created) == 1, f"the corrected pin created a second row: {api.created}"
    address_id, payload = api.updated[-1]
    assert address_id == 501
    assert payload["latitude"] == 41.3106
    assert payload["longitude"] == 69.2401


async def test_a_corrected_pin_that_lands_outside_the_zone_leaves_the_address_alone(
    handler, api
):
    """An out-of-zone pin is rejected before anything is stored, so the address
    the customer already earned keeps its previous, valid coordinates."""
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    out_of_zone = DummyUpdate()
    out_of_zone.message.location = DummyLocation(55.7558, 37.6173)  # Moscow
    await handler.location_received(out_of_zone, ctx)

    assert len(api.created) == 1
    assert all(
        payload.get("latitude") != 55.7558 for _id, payload in api.updated
    ), "an out-of-zone pin must never reach the backend"


async def test_a_failed_enrichment_at_the_end_still_reports_the_address_as_saved(
    handler, api, monkeypatch
):
    """Once the row exists, the terminal step is only adding optional details.
    Reporting "failed to save" there is worse than useless: the address IS
    saved, and a customer who believes otherwise walks the whole flow again and
    ends up with a duplicate.
    """
    ctx = make_context()
    await share_pin_and_name_it(handler, ctx)

    async def _refuse(_token, _address_id, _payload):
        return _Response(success=False, error="backend down", status_code=503)

    monkeypatch.setattr(api, "update_user_address", _refuse)

    update = tap("skip_delivery_instructions")
    assert await handler.skip_field_handler(update, ctx) == ConversationHandler.END

    shown = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert shown == "telegram.address.saved_successfully", (
        f"customer was told {shown!r} about an address that exists"
    )
