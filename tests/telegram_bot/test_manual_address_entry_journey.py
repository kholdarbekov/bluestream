"""The MANUAL address-entry journey — the branch that never sees a map pin.

WHY THIS FILE EXISTS
--------------------
`test_address_pin_flow_persistence.py` and `test_address_journey_dispatcher.py`
both drive the SHARED-PIN branch. The manual branch is the other half of the
same conversation and it runs the steps in the OPPOSITE order:

    pin    : location -> TITLE (address is created here) -> apartment ->
             floor -> instructions -> save
    manual : "Enter manually" -> region -> district -> street -> building ->
             apartment -> floor -> instructions -> GEOCODE -> confirm ->
             TITLE (address is created here) -> done

Same handlers, same states, mirrored semantics — `_is_shared_pin_address()` is
the only thing that tells them apart, and it is consulted in three different
places (`address_title_received`, `address_title_callback`,
`delivery_instructions_received`, `skip_field_handler`). A change that is
correct for the pin branch is therefore one boolean away from being wrong for
this one, and no test drove this branch end to end through the dispatcher.

Everything here goes in through `Application.process_update`, so the
conversation state machine, the handler groups and the real keyboards'
`callback_data` are all in the loop. Assertions are on what the customer SAW
(`bot.telegram`) or what reached the backend (`bot.backend.calls`) — never on
"a mock was called".

One thing worth knowing before reading any assertion here: the harness carries
the real callback-dedup middleware, because production registers it in
`_setup_handlers()` (moved there 2026-08-21). So an identical tap inside the
2-second window IS debounced exactly as in production, and journeys where a
real customer would have taken longer than that call
`let_the_dedup_window_lapse` first.

The second half of the file covers the address MANAGEMENT surfaces (view /
set-default / edit title / edit instructions / delete), because those are what
a customer reaches for after the flow above has produced something wrong.
"""

from __future__ import annotations

import pytest

# Bot modules resolve by bare name (tests/telegram_bot/conftest.py ranks
# telegram_bot/ first on sys.path). Imported at module level so `i18n`,
# `keyboards` and `config` are cached as the BOT's versions before anything
# else resolves them.
import utils as utils_module
from handlers import callback_dedup
from handlers.profile import (
    ADDRESS_APARTMENT,
    ADDRESS_BUILDING,
    ADDRESS_DELIVERY_INSTRUCTIONS,
    ADDRESS_DETAIL_MAX_LENGTH,
    ADDRESS_DISTRICT,
    ADDRESS_FLOOR,
    ADDRESS_GEOCODE_CONFIRM,
    ADDRESS_LOCATION,
    ADDRESS_REGION,
    ADDRESS_STREET,
    ADDRESS_TITLE,
)

from shared.constants import get_district_center, get_district_name

from tests.telegram_bot.ptb_harness import (
    FakeDatabase,
    backend_failure,
    build_bot_harness,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Copy the customer actually sees.
#
# Supplied rather than left to the harness's `humanised_missing_key` fallback
# for one load-bearing reason: `bot.py::_resolve_tapped_label` looks the
# manual-entry / re-enter / cancel REPLY-keyboard labels up when the tap
# arrives, from the same `i18n.get` the keyboard rendered them with. The table
# a test supplies is therefore also the table the router matches against — the
# same coupling production has, and the reason a mis-seeded key kills a
# reply-keyboard button there.
# ---------------------------------------------------------------------------

MANUAL_BUTTON = "Qo'lda kiritish"
REENTER_BUTTON = "Manzilni qayta kiritish"
CANCEL_BUTTON = "Bekor qilish"

UZ = {
    # --- the manual creation flow ---
    "telegram.address.location_prompt_enhanced": "Joylashuvni yuboring yoki manzilni yozing",
    "telegram.address.share_location_button": "Joylashuvni yuborish",
    "telegram.address.enter_manually_button": MANUAL_BUTTON,
    "telegram.address.reenter_manually_button": REENTER_BUTTON,
    "telegram.cancel": CANCEL_BUTTON,
    "telegram.address.manual_entry_started": "Manzilni qadam-baqadam kiritamiz",
    "telegram.address.select_region": "Viloyatni tanlang",
    "telegram.address.select_district": "Tumanni tanlang",
    "telegram.address.enter_street_required": "{district_name} tumani, ko'cha nomini yozing",
    "telegram.address.enter_building": "Uy raqamini yozing",
    "telegram.address.enter_apartment": "Xonadon raqamini yozing",
    "telegram.address.enter_floor": "Qavatni yozing",
    "telegram.address.enter_delivery_instructions": "Kuryer uchun izoh yozing",
    "telegram.address.skip_field": "Tashlab ketish",
    "telegram.address.field_too_long": "Juda uzun, {max_length} belgidan oshmasin",
    "telegram.address.geocode_found_with_address": "Topildi: {address}",
    "telegram.address.geocode_note_approximate_center": " (taxminiy nuqta)",
    "telegram.address.location_correct": "Hammasi to'g'ri",
    "telegram.address.location_wrong": "Joylashuv noto'g'ri",
    "telegram.address.retry_location": "Joylashuvni qayta yuboring yoki manzilni qayta yozing",
    "telegram.address.retry_location_toast": "Qaytadan urinamiz",
    "telegram.address.location_confirmed_toast": "Joylashuv tasdiqlandi",
    "telegram.address.title_prompt": "Bu manzilga nom bering",
    "telegram.address.saved_successfully": "Manzil saqlandi",
    "telegram.address.save_failed": "Manzilni saqlab bo'lmadi",
    "telegram.address.outside_delivery_area": "Bu joy yetkazish hududidan tashqarida",
    "telegram.action_cancelled": "Amal bekor qilindi",
    "telegram.action_cancelled_short": "Bekor qilindi",
    "telegram.common.processing": "Ishlanmoqda",
    "telegram.common.not_set": "korsatilmagan",
    # --- the management surfaces ---
    "telegram.address.list_header": "Sizda {count} ta manzil bor\n\n",
    "telegram.address.no_addresses": "Sizda hali manzil yo'q",
    "telegram.address.no_address_placeholder": "manzil korsatilmagan",
    "telegram.address.add_new": "Yangi manzil",
    "telegram.address.add_first": "Birinchi manzilni qo'shish",
    "telegram.address.edit": "Tahrirlash",
    "telegram.address.delete": "O'chirish",
    "telegram.back": "Orqaga",
    "telegram.address.details_title": "Manzil: {title}\n",
    "telegram.address.details_full_address": "To'liq: {address}\n",
    "telegram.address.details_street": "Ko'cha: {street}\n",
    "telegram.address.details_city": "Shahar: {city}\n",
    "telegram.address.details_default_badge": "Asosiy manzil\n",
    "telegram.address.set_default": "Asosiy qilish",
    "telegram.address.set_default_success_toast": "Asosiy manzil yangilandi",
    "telegram.address.not_found": "Manzil topilmadi",
    "telegram.address.untitled": "Nomsiz",
    "telegram.address.delete_confirmation": "{title} ({address}) o'chirilsinmi?",
    "telegram.address.delete_confirm_yes": "Ha, o'chirilsin",
    "telegram.address.deleted_success_toast": "Manzil o'chirildi",
    "telegram.address.delete_failed_toast": "O'chirib bo'lmadi",
    "telegram.address.delete_failed_detail": "O'chirib bo'lmadi: {error}",
    "telegram.address.edit_options_text": "Nimani o'zgartiramiz?",
    "telegram.address.edit_title_button": "Nomi",
    "telegram.address.edit_location_button": "Joylashuvi",
    "telegram.address.edit_details_button": "Tafsilotlari",
    "telegram.address.edit_instructions_button": "Izohlari",
    "telegram.address.delete_readd_button": "O'chirib qayta qo'shish",
    "telegram.address.edit_title_prompt": "Hozirgi nom: {current_title}. Yangi nom yozing",
    "telegram.address.title_updated_success": "Nom yangilandi: {title}",
    "telegram.address.title_too_short": "Nom juda qisqa",
    "telegram.address.title_too_long": "Nom juda uzun",
    "telegram.address.edit_instructions_prompt": "Hozirgi izoh: {current_instructions}. Yangi izoh yozing",
    "telegram.address.instructions_updated_intro": "Izoh yangilandi. ",
    "telegram.address.instructions_new_value": "Yangi izoh: {value}",
    "telegram.address.instructions_too_long": "Izoh juda uzun",
    "telegram.address.none_value": "yo'q",
    "telegram.address.no_addresses_to_delete": "O'chirish uchun manzil yo'q",
    "telegram.address.location_edit_not_supported": (
        "Joylashuvni o'zgartirib bo'lmaydi, manzilni o'chirib qayta qo'shing"
    ),
    "telegram.address.details_edit_coming_soon": "Tafsilotlarni tahrirlash tez orada",
}


DISTRICT_KEY = "chilanzar"
DISTRICT_UZ = get_district_name(DISTRICT_KEY, "uz")
DISTRICT_CENTRE = get_district_center(DISTRICT_KEY)

# What the geocoder answers for the manual address below. Inside
# TASHKENT_POLYGON, so the real delivery-zone SSOT accepts it.
GEOCODED_LAT = 41.2811
GEOCODED_LNG = 69.1839
GEOCODED_ADDRESS = "Bunyodkor kochasi 15, Chilonzor, Toshkent"


class AddressBookDatabase(FakeDatabase):
    """`FakeDatabase` that actually serves ``bot_state`` back.

    The stock harness answers ``SELECT bot_state FROM users`` with ``None``, so
    every ``awaiting_input`` the bot writes is invisible on read-back and the
    customer's reply to an edit prompt falls through to the group-0 catch-all
    and is filed as a SUPPORT TICKET. That is not what production does — the
    column round-trips there — and testing the title/instructions edit journeys
    against the stock fake would prove the opposite of the truth.
    """

    async def fetchval(self, query, *args):
        if "bot_state" in query:
            return self.user.get("bot_state")
        return await super().fetchval(query, *args)


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(
        monkeypatch, translations=UZ, database=AddressBookDatabase()
    )

    # The group-0 text catch-all (`_handle_text_message`) consults the real
    # Redis-backed rate limiter, which FAILS CLOSED when Redis is unreachable.
    # That is genuine external I/O and the only seam in the text path the
    # harness does not already own.
    async def _always_allow(_user_id):
        return True

    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allow)

    harness.backend.route(
        "POST",
        "/api/v1/addresses/geocode",
        lambda _call: {
            "data": {
                "latitude": GEOCODED_LAT,
                "longitude": GEOCODED_LNG,
                "formatted_address": GEOCODED_ADDRESS,
            }
        },
    )
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


# ---------------------------------------------------------------------------
# Small vocabulary for reading the journeys below
# ---------------------------------------------------------------------------


def creates(bot):
    """Every address CREATE that reached the backend, newest last."""
    return [
        call
        for call in bot.backend.calls
        if call.method == "POST" and call.endpoint == "/api/v1/auth/addresses"
    ]


def updates(bot):
    """Every address UPDATE that reached the backend, as (id, payload)."""
    return [
        (int(call.endpoint.rsplit("/", 1)[-1]), call.data)
        for call in bot.backend.calls
        if call.method == "PUT" and call.endpoint.startswith("/api/v1/auth/addresses/")
    ]


def deletes(bot):
    return [
        int(call.endpoint.rsplit("/", 1)[-1])
        for call in bot.backend.calls
        if call.method == "DELETE" and call.endpoint.startswith("/api/v1/auth/addresses/")
    ]


def geocode_requests(bot):
    return [
        call.data
        for call in bot.backend.calls
        if call.endpoint == "/api/v1/addresses/geocode"
    ]


def support_posts(bot):
    return [
        call.data
        for call in bot.backend.calls
        if call.endpoint == "/api/v1/support/messages"
    ]


def state(bot):
    return bot.conversation_state("address_conversation")


def answered_toasts(bot):
    """The toast text of every answerCallbackQuery the bot sent."""
    return [call.params.get("text", "") for call in bot.telegram.of("answerCallbackQuery")]


def let_the_dedup_window_lapse():
    """Represent a customer who took longer than two seconds over a step.

    `callback_dedup` debounces (user_id, callback_data) for 2 seconds, so a
    journey that legitimately revisits a step — walking region/district a
    SECOND time after saying the pin was wrong — would have its second tap
    dropped simply because a test runs in milliseconds.

    This is load-bearing, not decorative: since 2026-08-21 production registers
    the middleware in `_setup_handlers()`, which is what the harness builds, so
    the debounce really is in the loop. Ageing the lock table is what the wall
    clock would do, without putting two real seconds into the suite.
    """
    callback_dedup._in_memory_locks.clear()


def acting_handlers(bot, update):
    """The names of the handlers that would DO something with this update.

    The exclusion is load-bearing, and since 2026-08-21 the harness owns it:
    the dispatcher registers three handlers that claim EVERY update and act on
    none — the debug-logger and callback-dedup `TypeHandler`s, and a
    pattern-less `CallbackQueryHandler` at group -1 — so counting any of them
    would make "does this button land anywhere" pass for a completely dead
    button. `handlers_matching()` drops them; the control below proves it.
    """
    catch_alls = bot.handlers_matching(update, include_catch_alls=True)
    assert len(catch_alls) > len(bot.handlers_matching(update)), (
        "the catch-alls this oracle relies on being excluded are gone; every "
        "'does this button land anywhere' check below is now vacuous"
    )
    return [
        getattr(getattr(handler, "callback", None), "__name__", type(handler).__name__)
        for _group, handler in bot.handlers_matching(update)
    ]


async def start_manual_entry(bot, user):
    """Open the address flow and choose "Enter manually" over the map pin."""
    await bot.send(user.tap("add_new_address"))
    assert state(bot) == ADDRESS_LOCATION

    await bot.send(user.text(MANUAL_BUTTON))
    assert state(bot) == ADDRESS_REGION, (
        "the 'Enter manually' reply-keyboard label did not match the regex "
        "bot.py compiled from telegram.address.enter_manually_button"
    )


async def walk_to_the_street_prompt(bot, user):
    await start_manual_entry(bot, user)
    await bot.send(user.tap("region_tashkent_city"))
    assert state(bot) == ADDRESS_DISTRICT
    await bot.send(user.tap(f"district_{DISTRICT_KEY}"))
    assert state(bot) == ADDRESS_STREET


async def walk_to_the_geocode_confirmation(bot, user):
    """The whole typed chain: street, building, apartment, floor, instructions."""
    await walk_to_the_street_prompt(bot, user)
    await bot.send(user.text("Bunyodkor"))
    assert state(bot) == ADDRESS_BUILDING
    await bot.send(user.text("15"))
    assert state(bot) == ADDRESS_APARTMENT
    await bot.send(user.text("45"))
    assert state(bot) == ADDRESS_FLOOR
    await bot.send(user.text("9"))
    assert state(bot) == ADDRESS_DELIVERY_INSTRUCTIONS
    await bot.send(user.text("domofon 45"))
    assert state(bot) == ADDRESS_GEOCODE_CONFIRM


# ===========================================================================
# The happy path
# ===========================================================================


async def test_typing_an_address_by_hand_saves_it_with_exactly_the_columns_the_customer_filled(
    bot, user
):
    """The manual flow's ONE write. It happens at the title step — last, not
    first as in the pin flow — so every earlier answer has to have survived
    seven state transitions in volatile `user_data` to get here.

    Asserting the exact payload (not "a POST happened") is the point: this is
    the dict the driver's route card is built from. A field silently dropped
    between `_build_address_payload` and the backend is invisible to a
    count-based assertion and lands as a courier ringing the wrong doorbell.
    """
    await walk_to_the_geocode_confirmation(bot, user)

    await bot.send(user.tap("confirm_geocode"))
    assert state(bot) == ADDRESS_TITLE

    await bot.send(user.tap("addr_title_home"))

    assert state(bot) is None, "the flow must be over once the address is named"
    assert len(creates(bot)) == 1, f"expected exactly one create, got {creates(bot)}"
    assert creates(bot)[0].data == {
        "title": "Uy",
        "full_address": GEOCODED_ADDRESS,
        "street_address": "Bunyodkor",
        "city": "Tashkent",
        "district": DISTRICT_KEY,
        "latitude": GEOCODED_LAT,
        "longitude": GEOCODED_LNG,
        "apartment_number": "45",
        "floor_number": "9",
        "delivery_instructions": "domofon 45",
    }
    assert bot.telegram.last_shown().text == UZ["telegram.address.saved_successfully"]


async def test_the_geocode_request_carries_the_building_number_and_the_district_centre_hint(
    bot, user
):
    """`UserAddress` has no building column, so the house number the customer
    typed reaches the backend ONLY inside the geocode query string (and from
    there into `full_address`). Lose it from that string and the flow still
    saves — at the geocoder's guess for the street, i.e. the wrong end of a
    two-kilometre road.

    The hint pins the geocoder to the chosen district; without it "Bunyodkor"
    resolves anywhere in the country.
    """
    await walk_to_the_geocode_confirmation(bot, user)

    assert geocode_requests(bot) == [
        {
            "address": f"Bunyodkor street, 15, {DISTRICT_UZ}, Tashkent, Uzbekistan",
            "hint_lat": DISTRICT_CENTRE[0],
            "hint_lon": DISTRICT_CENTRE[1],
        }
    ]
    # And the customer is shown the pin before being asked to confirm it.
    assert bot.telegram.of("sendLocation"), "no map pin was sent to confirm"
    assert bot.telegram.last_shown().text == UZ[
        "telegram.address.geocode_found_with_address"
    ].format(address=GEOCODED_ADDRESS)


async def test_skipping_the_building_never_asks_for_an_apartment_or_a_floor(bot, user):
    """No building means a private house: `_SKIP_TARGETS` jumps building ->
    delivery_instructions on purpose. If that mapping regresses to
    building -> apartment, a customer with no building is asked which flat in
    their detached house they live in, and answers nothing — which is how a
    flow gets abandoned.
    """
    await walk_to_the_street_prompt(bot, user)
    await bot.send(user.text("Bunyodkor"))

    await bot.send(user.tap("skip_building"))
    assert state(bot) == ADDRESS_DELIVERY_INSTRUCTIONS

    shown = bot.telegram.texts()
    assert UZ["telegram.address.enter_apartment"] not in shown
    assert UZ["telegram.address.enter_floor"] not in shown

    await bot.send(user.tap("skip_delivery_instructions"))
    assert state(bot) == ADDRESS_GEOCODE_CONFIRM
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_other"))

    # A skipped field must be ABSENT, not empty-stringed: `_build_address_payload`
    # drops Nones so the backend never writes a column the customer never filled.
    assert creates(bot)[0].data == {
        "title": "Boshqa",
        "full_address": GEOCODED_ADDRESS,
        "street_address": "Bunyodkor",
        "city": "Tashkent",
        "district": DISTRICT_KEY,
        "latitude": GEOCODED_LAT,
        "longitude": GEOCODED_LNG,
    }
    assert geocode_requests(bot) == [
        {
            "address": f"Bunyodkor street, {DISTRICT_UZ}, Tashkent, Uzbekistan",
            "hint_lat": DISTRICT_CENTRE[0],
            "hint_lon": DISTRICT_CENTRE[1],
        }
    ]


async def test_a_geocoder_outage_falls_back_to_the_district_centre_and_admits_it(bot, user):
    """The geocoder is a third party and it does go down. When it does the
    flow must still produce a deliverable address — the district centre, with
    the customer TOLD it is approximate — instead of dead-ending five steps in.

    The fallback coordinates come from the real `get_district_center`, so a
    district added without a centre is caught here rather than by a courier.
    """
    bot.backend.route(
        "POST",
        "/api/v1/addresses/geocode",
        lambda _call: backend_failure("geocoder unavailable", status_code=503),
    )

    await walk_to_the_geocode_confirmation(bot, user)

    expected_string = f"Bunyodkor street, 15, {DISTRICT_UZ}, Tashkent, Uzbekistan"
    assert bot.telegram.last_shown().text == (
        UZ["telegram.address.geocode_found_with_address"].format(address=expected_string)
        + UZ["telegram.address.geocode_note_approximate_center"]
    ), "the customer was not warned the pin is only the district centre"

    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_work"))

    saved = creates(bot)[0].data
    assert (saved["latitude"], saved["longitude"]) == DISTRICT_CENTRE
    assert saved["full_address"] == expected_string
    assert saved["title"] == "Ish"


async def test_an_over_long_apartment_or_floor_is_refused_without_costing_the_customer_the_flow(
    bot, user
):
    """`apartment_number` / `floor_number` are String(20). A longer answer
    reaches Postgres as a DataError, the save 500s, and everything typed over
    seven steps is gone. So the bot must refuse it AT THE STEP, keep the
    customer in that state, and re-render the Skip button — otherwise the
    re-prompt is a dead end with no way past it.
    """
    await walk_to_the_street_prompt(bot, user)
    await bot.send(user.text("Bunyodkor"))
    await bot.send(user.text("15"))

    too_long = "x" * (ADDRESS_DETAIL_MAX_LENGTH + 1)
    await bot.send(user.text(too_long))

    assert state(bot) == ADDRESS_APARTMENT, "an over-long answer must not advance"
    rejection = bot.telegram.last_shown()
    assert rejection.text == UZ["telegram.address.field_too_long"].format(
        max_length=ADDRESS_DETAIL_MAX_LENGTH
    )
    assert rejection.callback_data() == ["skip_apartment"], (
        "the re-prompt dropped the Skip button, so a customer who cannot "
        "shorten their answer is trapped"
    )

    await bot.send(user.text("45"))
    assert state(bot) == ADDRESS_FLOOR

    await bot.send(user.text("9" * (ADDRESS_DETAIL_MAX_LENGTH + 5)))
    assert state(bot) == ADDRESS_FLOOR
    assert bot.telegram.last_shown().callback_data() == ["skip_floor"]

    await bot.send(user.text("9"))
    await bot.send(user.text("domofon 45"))
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_home"))

    saved = creates(bot)[0].data
    assert saved["apartment_number"] == "45"
    assert saved["floor_number"] == "9"
    assert too_long not in saved.values()


# ===========================================================================
# "This pin is wrong" — the retry loop, where the chain runs TWICE
# ===========================================================================


async def test_saying_the_pin_is_wrong_reopens_manual_entry_from_the_top(bot, user):
    """`retry_geocode` is the only way back out of the confirmation screen. It
    must return the customer to ADDRESS_LOCATION with BOTH doors open — share a
    pin, or retype the address — because the reason the geocode was wrong is
    usually that the typed street was wrong.

    A tap that leaves them in ADDRESS_GEOCODE_CONFIRM would loop them onto the
    same wrong pin forever.
    """
    await walk_to_the_geocode_confirmation(bot, user)

    await bot.send(user.tap("retry_geocode"))

    assert state(bot) == ADDRESS_LOCATION
    retry_prompt = bot.telegram.last_shown()
    assert retry_prompt.text == UZ["telegram.address.retry_location"]
    assert retry_prompt.button_labels() == [
        UZ["telegram.address.share_location_button"],
        REENTER_BUTTON,
        CANCEL_BUTTON,
    ]
    assert UZ["telegram.address.retry_location_toast"] in answered_toasts(bot)

    # The re-enter label is a DIFFERENT translation key from the one that
    # opened the flow; both must reach skip_location_sharing.
    let_the_dedup_window_lapse()
    await bot.send(user.text(REENTER_BUTTON))
    assert state(bot) == ADDRESS_REGION


async def test_a_skip_on_the_second_pass_clears_what_the_customer_typed_on_the_first(
    bot, user
):
    """`retry_geocode` reruns the WHOLE detail chain over `temp_address_data`
    that is still populated from the first pass. So Skip cannot mean "leave it
    alone" — it has to CLEAR, or a customer who corrects "flat 45, floor 9" to
    "no flat, no floor" is delivered to flat 45 anyway.

    This is the exact reason `_ADDRESS_FIELD_DATA_KEYS` exists.
    """
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("retry_geocode"))
    let_the_dedup_window_lapse()

    await bot.send(user.text(REENTER_BUTTON))
    await bot.send(user.tap("region_tashkent_city"))
    await bot.send(user.tap(f"district_{DISTRICT_KEY}"))
    await bot.send(user.text("Amir Temur"))
    await bot.send(user.text("7"))

    assert state(bot) == ADDRESS_APARTMENT
    await bot.send(user.tap("skip_apartment"))
    assert state(bot) == ADDRESS_FLOOR
    await bot.send(user.tap("skip_floor"))
    assert state(bot) == ADDRESS_DELIVERY_INSTRUCTIONS

    await bot.send(user.text("eshik yonida qoldiring"))
    await bot.send(user.tap("confirm_geocode"))
    let_the_dedup_window_lapse()
    await bot.send(user.tap("addr_title_home"))

    saved = creates(bot)[0].data
    assert "apartment_number" not in saved, (
        "flat 45 from the FIRST pass survived a Skip on the second — the "
        "customer corrected their address and was delivered to the old flat"
    )
    assert "floor_number" not in saved
    assert saved["street_address"] == "Amir Temur"
    assert saved["delivery_instructions"] == "eshik yonida qoldiring"


async def test_skipping_the_building_on_the_second_pass_clears_the_stale_flat_and_floor(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    Skipping the building is defined as "private house", and `_SKIP_TARGETS`
    therefore jumps straight past apartment and floor. But `skip_field_handler`
    used to clear only the key of the field it was tapped on, so after a
    `retry_geocode` the apartment and floor typed on the FIRST pass were never
    cleared and were saved onto a house that, by the customer's own answer, has
    neither.

    WHAT THE FIX GUARANTEES: `_cleared_by_skip` walks `_ADDRESS_FIELD_DATA_KEYS`
    in flow order from the field tapped up to the field the jump LANDS on, so a
    Skip clears everything it skipped — exactly what the
    `_ADDRESS_FIELD_DATA_KEYS` docstring promises.
    """
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("retry_geocode"))
    let_the_dedup_window_lapse()

    await bot.send(user.text(REENTER_BUTTON))
    await bot.send(user.tap("region_tashkent_city"))
    await bot.send(user.tap(f"district_{DISTRICT_KEY}"))
    await bot.send(user.text("Amir Temur"))

    assert state(bot) == ADDRESS_BUILDING
    await bot.send(user.tap("skip_building"))
    assert state(bot) == ADDRESS_DELIVERY_INSTRUCTIONS, (
        "no building means no flat and no floor to ask about"
    )

    await bot.send(user.tap("skip_delivery_instructions"))
    await bot.send(user.tap("confirm_geocode"))
    let_the_dedup_window_lapse()
    await bot.send(user.tap("addr_title_home"))

    saved = creates(bot)[0].data
    assert "apartment_number" not in saved, (
        "the flat typed before the retry survived a building Skip that is "
        "documented as skipping apartment and floor"
    )
    assert "floor_number" not in saved, "same for floor"
    assert "building_number" not in saved, "and the building that was skipped"


async def test_both_ways_into_the_geocode_step_enforce_the_same_delivery_zone(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    `geocode_and_confirm` (customer TYPED delivery instructions) enforces
    `is_within_tashkent` and re-prompts with the localized "outside delivery
    area" message. `geocode_and_confirm_callback` (customer TAPPED Skip) was
    the same code with that guard missing, so which of two equivalent buttons
    the customer pressed decided whether the delivery-zone SSOT was enforced.

    Consequence: the Skip customer was shown an out-of-zone pin as if it were
    fine, named it, and only then got the generic "could not save" — the
    backend's `_enforce_address_delivery_zone` backstop rejecting it with no
    explanation and no way back.

    WHAT THE FIX GUARANTEES: there is only ONE function now
    (`geocode_and_confirm(..., is_callback=)`), so there is only one expression
    of the guard and only one place it can go missing from. Both entry points
    reject the same point with the same localized message and send the customer
    back to ADDRESS_LOCATION.
    """
    moscow = {"latitude": 55.7558, "longitude": 37.6173, "formatted_address": "Moskva"}
    bot.backend.route(
        "POST", "/api/v1/addresses/geocode", lambda _call: {"data": moscow}
    )

    # Branch A — the customer TYPES the delivery instructions. Guard fires.
    await walk_to_the_street_prompt(bot, user)
    await bot.send(user.text("Bunyodkor"))
    await bot.send(user.text("15"))
    await bot.send(user.text("45"))
    await bot.send(user.text("9"))
    await bot.send(user.text("domofon 45"))

    assert state(bot) == ADDRESS_LOCATION, "typed branch must reject and re-ask"
    assert bot.telegram.last_shown().text == UZ["telegram.address.outside_delivery_area"]

    # Branch B — same address, but the customer TAPS Skip. Guard is absent.
    let_the_dedup_window_lapse()
    await bot.send(user.text(MANUAL_BUTTON))
    await bot.send(user.tap("region_tashkent_city"))
    await bot.send(user.tap(f"district_{DISTRICT_KEY}"))
    await bot.send(user.text("Bunyodkor"))
    await bot.send(user.text("15"))
    await bot.send(user.text("45"))
    await bot.send(user.text("9"))
    await bot.send(user.tap("skip_delivery_instructions"))

    assert state(bot) == ADDRESS_LOCATION, (
        "the Skip branch walked an out-of-zone point straight to the "
        "confirmation screen"
    )
    assert bot.telegram.last_shown().text == UZ["telegram.address.outside_delivery_area"], (
        "the Skip customer must be told why, in their own language, exactly as "
        "the typed customer is"
    )
    assert not any(
        call.text.startswith("Topildi: Moskva") for call in bot.telegram.shown
    ), "a Moscow pin must never be offered as a valid delivery address"
    assert creates(bot) == [], "and nothing out of zone may be written"


# ===========================================================================
# Giving up
# ===========================================================================


async def test_cancelling_at_the_confirmation_throws_away_everything_and_ends_cleanly(
    bot, user
):
    """Cancel at the LAST step, with seven answers already collected, is the
    most expensive exit in the flow. Nothing may be written (the manual branch
    has created nothing yet, unlike the pin branch), the conversation must end,
    and — the part that bites — the flow's `user_data` must be gone, because a
    stale `temp_address_data` is what a later save silently merges into.
    """
    await walk_to_the_geocode_confirmation(bot, user)

    await bot.send(user.tap("cancel_address_creation"))

    assert state(bot) is None
    assert creates(bot) == [], "a cancelled manual address must never be written"
    assert bot.telegram.last_shown().text == UZ["telegram.action_cancelled"]

    # Proof the flow really ended rather than merely looking ended: the next
    # thing typed is no longer a street name to this bot.
    await bot.send(user.text("Bunyodkor"))
    assert state(bot) is None
    assert creates(bot) == []


async def test_the_cancel_command_rescues_a_customer_stuck_at_the_district_step(bot, user):
    """The district screen renders Back and Cancel, but a customer who has
    scrolled the keyboard away reaches for /cancel. It is a conversation
    FALLBACK, so it must work from any state — a flow you can only leave by
    finding the right button is a flow customers abandon with the bot still
    holding their next message hostage.
    """
    await start_manual_entry(bot, user)
    await bot.send(user.tap("region_tashkent_city"))
    assert state(bot) == ADDRESS_DISTRICT

    await bot.send(user.command("cancel"))

    assert state(bot) is None
    assert creates(bot) == []


async def test_backing_out_of_the_district_list_returns_to_the_region_list(bot, user):
    """Back is a navigation button, not an exit: it must land in ADDRESS_REGION
    with the region keyboard live again. A Back that ends the conversation
    instead leaves the customer tapping a dead region button.
    """
    await start_manual_entry(bot, user)
    await bot.send(user.tap("region_tashkent_city"))

    await bot.send(user.tap("back_to_region"))

    assert state(bot) == ADDRESS_REGION
    back_at_regions = bot.telegram.last_shown()
    assert back_at_regions.text == UZ["telegram.address.select_region"]
    assert "region_tashkent_city" in back_at_regions.callback_data()

    let_the_dedup_window_lapse()
    await bot.send(user.tap("region_tashkent_city"))
    assert state(bot) == ADDRESS_DISTRICT, "the region button was dead after Back"


# ===========================================================================
# The dispatcher itself misbehaving
# ===========================================================================


async def test_every_button_the_manual_flow_renders_is_claimed_by_a_working_handler(
    bot, user
):
    """A tap no handler claims shows a spinner and then nothing — the most
    common way a Telegram flow dies silently. Walk the manual flow and check,
    at each step, that every button on the message the customer is looking at
    reaches a handler that will DO something.

    Buttons are checked in the conversation state they were RENDERED in, which
    is the part a keyboard-only unit test cannot see: `skip_apartment` is a
    perfectly good callback_data that is dead everywhere except ADDRESS_APARTMENT.
    """
    journey = [
        (user.text(MANUAL_BUTTON), "on the region screen"),
        (user.tap("region_tashkent_city"), "on the district screen"),
        (user.tap(f"district_{DISTRICT_KEY}"), "on the street prompt"),
        (user.text("Bunyodkor"), "on the building prompt"),
        (user.text("15"), "on the apartment prompt"),
        (user.text("45"), "on the floor prompt"),
        (user.text("9"), "on the delivery-instructions prompt"),
        (user.text("domofon 45"), "on the geocode confirmation"),
        (user.tap("confirm_geocode"), "on the title prompt"),
    ]

    await bot.send(user.tap("add_new_address"))

    for update, where in journey:
        bot.telegram.reset()
        await bot.send(update)

        rendered = bot.telegram.shown
        assert rendered, f"the bot showed nothing {where}"

        for data in rendered[-1].callback_data():
            assert acting_handlers(bot, user.tap(data)), (
                f"the {data!r} button rendered {where} lands nowhere: no "
                f"registered handler claims it in this conversation state"
            )


async def test_a_telegram_edit_rejection_at_the_district_step_keeps_the_flow_alive(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    `district_selected` renders the street prompt by editing the district
    message. "Message to edit not found" is a routine Telegram rejection (the
    customer deleted the bubble, or a previous handler already replaced it) and
    this project's production logs carry it. The handler's blanket `except
    Exception` turned it into `ConversationHandler.END`: no message, no toast,
    no flow. The customer then typed their street into a bot that was no longer
    listening, and the group-0 catch-all filed it as a SUPPORT TICKET with no
    acknowledgement.

    WHAT THE FIX GUARANTEES: a failed edit is a rendering problem, not a flow
    problem. The manual-entry steps render through
    `handlers.base._edit_or_replace_callback_message`, which falls back to
    sending a NEW message — so the street prompt still arrives, the flow stays
    in ADDRESS_STREET, and the street name is consumed by the conversation
    instead of the support inbox.
    """
    await start_manual_entry(bot, user)
    await bot.send(user.tap("region_tashkent_city"))

    bot.telegram.fail("editMessageText", "Bad Request: message to edit not found")
    bot.telegram.reset()
    await bot.send(user.tap(f"district_{DISTRICT_KEY}"))
    bot.telegram.clear_failures()

    assert state(bot) == ADDRESS_STREET, (
        "one rejected edit must not end the whole address flow"
    )
    replacement = bot.telegram.last_shown()
    assert replacement.method == "sendMessage", (
        "the prompt the edit could not deliver must be resent as a new message"
    )
    assert "ko'cha nomini yozing" in replacement.text, (
        f"the customer must still be asked for their street; got {replacement.text!r}"
    )

    bot.telegram.reset()
    await bot.send(user.text("Bunyodkor"))

    assert state(bot) == ADDRESS_BUILDING, "the street name reached the flow"
    assert bot.telegram.last_shown().text == UZ["telegram.address.enter_building"]
    assert support_posts(bot) == [], (
        "an answer the conversation asked for must never reach the support inbox"
    )


async def test_no_typed_address_answer_is_ever_filed_as_a_support_ticket(bot, user):
    """Regression guard: the manual branch types the MOST, so it leaked the most.

    The address conversation lives in group -2 and the free-text catch-all
    `_handle_text_message` in group 0. PTB runs at most one handler PER GROUP
    and then carries on to the next group, and `ConversationHandler` only
    re-raises `ApplicationHandlerStop` for a handler that raised one itself —
    so while none did, every plain-text answer inside the flow was handled
    TWICE: once by the address step, and once by the catch-all, which found no
    `awaiting_input` (the flow's own `add_address` cleared it) and silently
    filed the text as a support message.

    Live consequence: the support inbox filled with "Bunyodkor", "15", "45",
    "9" from every customer who typed an address, each looking like a question
    an operator ignored — while the customer, who was never told a ticket was
    opened, was never answered. This branch is where it was worst: six typed
    answers, six tickets, one address.

    WHAT THE FIX GUARANTEES: each of those steps is registered through
    `WaterBusinessBot._consumes`, so the update stops in group -2. The
    "Enter manually" reply-keyboard tap counts too — it arrives as TEXT and was
    the first thing filed.
    """
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_home"))

    assert len(creates(bot)) == 1, "the address itself still saves"
    assert support_posts(bot) == [], (
        "every typed step of the manual address flow used to be duplicated "
        "into the admin Support Inbox"
    )


async def test_losing_the_session_at_the_final_save_keeps_every_answer_for_a_retry(
    bot, user
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    `save_address_final` used to clear `temp_address_data` BEFORE it knew
    whether the write had succeeded. When the token had expired and
    re-authentication failed — a transient backend blip — the customer was told
    "could not save" and the seven answers behind it were gone from memory with
    no retry offered: the only way forward was to walk the whole flow again.

    WHAT THE FIX GUARANTEES: nothing is discarded until something is written.
    The answers stay in `temp_address_data`, the conversation stays in
    ADDRESS_TITLE with the title buttons live, and one more tap — once the
    backend is back — saves everything the customer already typed.
    """
    await walk_to_the_geocode_confirmation(bot, user)
    await bot.send(user.tap("confirm_geocode"))

    # The session dies between the confirmation and the title tap.
    restored_token_manager = bot.application.bot_data["token_manager"]

    class _ExpiredTokenManager:
        redis = None

        async def get_valid_token(self, *_args, **_kwargs):
            return None

        async def store_tokens(self, *_args, **_kwargs):
            return True

        async def invalidate_tokens(self, *_args, **_kwargs):
            return True

    bot.application.bot_data["token_manager"] = _ExpiredTokenManager()
    bot.backend.route(
        "POST",
        "/api/v1/auth/telegram-login",
        lambda _call: backend_failure("token expired", status_code=401),
    )

    await bot.send(user.tap("addr_title_home"))

    assert creates(bot) == [], "no token means no write"
    failure = bot.telegram.last_shown()
    assert failure.text == UZ["telegram.address.save_failed"]
    assert "addr_title_home" in failure.callback_data(), (
        "the failure screen has to carry the buttons that retry the save"
    )
    assert state(bot) == ADDRESS_TITLE, (
        "the conversation must survive a failed save, or the title button the "
        "customer retaps is no longer wired to anything"
    )

    # The session comes back. One tap, and the seven answers behind it are
    # saved — none of them retyped.
    bot.application.bot_data["token_manager"] = restored_token_manager
    bot.backend.routes.pop(("POST", "/api/v1/auth/telegram-login"), None)
    let_the_dedup_window_lapse()

    await bot.send(user.tap("addr_title_home"))

    assert state(bot) is None, "the retry completes the flow"
    (saved,) = creates(bot)
    assert saved.data["street_address"] == "Bunyodkor"
    assert saved.data["apartment_number"] == "45"
    assert saved.data["floor_number"] == "9"
    assert saved.data["delivery_instructions"] == "domofon 45"
    assert bot.telegram.last_shown().text == UZ["telegram.address.saved_successfully"]


async def test_switching_language_mid_flow_still_saves_and_names_the_address_in_the_new_language(
    bot, user
):
    """Customers do change language mid-flow (the profile menu is two taps
    away). The title suggestions are rendered from a per-language map at TAP
    time, so the saved title must follow the language in force then — while the
    district name captured earlier stays as it was captured, because it is a
    snapshot fed to the geocoder, not display copy.

    If either fact regresses the address is saved under a label the customer
    never chose, or the geocode query changes shape between passes.
    """
    await walk_to_the_street_prompt(bot, user)

    # Two taps in the profile menu later...
    bot.database.user["preferred_language"] = "ru"

    await bot.send(user.text("Bunyodkor"))
    await bot.send(user.text("15"))
    await bot.send(user.tap("skip_apartment"))
    await bot.send(user.tap("skip_floor"))
    await bot.send(user.tap("skip_delivery_instructions"))
    await bot.send(user.tap("confirm_geocode"))
    await bot.send(user.tap("addr_title_home"))

    saved = creates(bot)[0].data
    assert saved["title"] == "Дом", "the title was rendered in the abandoned language"
    assert geocode_requests(bot)[0]["address"].split(", ")[2] == DISTRICT_UZ, (
        "the district name is a snapshot taken when it was picked; re-deriving "
        "it later would change the geocode query under the customer"
    )


# ===========================================================================
# Address management — what the customer does when the flow above got it wrong
# ===========================================================================


@pytest.fixture
def address_book(bot):
    """Two saved addresses, the first of them default."""
    bot.backend.addresses.update(
        {
            901: {
                "id": 901,
                "title": "Uy",
                "full_address": "Bunyodkor 15, Chilonzor",
                "street_address": "Bunyodkor",
                "city": "Toshkent",
                "district": "chilanzar",
                "is_default": True,
                "delivery_instructions": "domofon 15",
            },
            902: {
                "id": 902,
                "title": "Ish",
                "full_address": "Amir Temur 7, Mirobod",
                "street_address": "Amir Temur",
                "city": "Toshkent",
                "district": "mirobod",
                "is_default": False,
                "delivery_instructions": None,
            },
        }
    )

    # The stock fake answers DELETE with success even for an id it does not
    # hold. The real endpoint 404s (business_app/api/auth.py::delete_user_address),
    # and the difference is the whole content of the stale-button and re-tap
    # journeys below.
    def _delete(call):
        address_id = int(call.endpoint.rsplit("/", 1)[-1])
        if address_id not in bot.backend.addresses:
            return backend_failure("Address not found", status_code=404)
        bot.backend.addresses.pop(address_id)
        return {"data": {}}

    for address_id in (901, 902):
        bot.backend.route("DELETE", f"/api/v1/auth/addresses/{address_id}", _delete)

    return bot.backend.addresses


async def test_the_address_list_renders_one_live_button_per_saved_address(
    bot, user, address_book
):
    """The list is the entry point to every other management action. Its
    buttons carry the address id inside `callback_data`, so an off-by-one in
    how that id is spliced in or split back out sends the customer to somebody
    else's address — and there is no confirmation step on view or set-default.
    """
    await bot.send(user.tap("manage_addresses"))

    listing = bot.telegram.last_shown()
    assert listing.text.startswith(
        UZ["telegram.address.list_header"].format(count=2)
    )
    assert "Uy" in listing.text and "Bunyodkor 15, Chilonzor" in listing.text
    assert "Ish" in listing.text and "Amir Temur 7, Mirobod" in listing.text
    assert listing.callback_data() == [
        "view_address_901",
        "view_address_902",
        "add_new_address",
        "select_edit_address",
        "select_delete_address",
        "menu_profile",
    ]

    await bot.send(user.tap("view_address_902"))
    detail = bot.telegram.last_shown()
    assert detail.text == (
        "Manzil: Ish\n"
        "To'liq: Amir Temur 7, Mirobod\n"
        "Ko'cha: Amir Temur\n"
        "Shahar: Toshkent\n"
    )
    assert detail.callback_data() == [
        "set_default_address_902",
        "edit_address_902",
        "delete_address_902",
        "manage_addresses",
    ]


async def test_making_the_other_address_default_moves_the_badge_and_hides_the_button(
    bot, user, address_book
):
    """Set-default has no confirmation, so the only feedback the customer gets
    is the refreshed detail screen. If it does not re-render, they tap again —
    and every extra tap is another write. The button must also DISAPPEAR once
    the address is default: an address cannot be made default twice, and a
    button that does nothing is indistinguishable from a broken bot.
    """
    patched = []

    def _set_default(call):
        patched.append(call)
        for row in address_book.values():
            row["is_default"] = row["id"] == 902
        return {"data": {"address": address_book[902]}}

    bot.backend.route(
        "PATCH", "/api/v1/auth/addresses/902/set-default", _set_default
    )

    await bot.send(user.tap("set_default_address_902"))

    assert len(patched) == 1
    assert address_book[902]["is_default"] is True
    assert address_book[901]["is_default"] is False
    assert UZ["telegram.address.set_default_success_toast"] in answered_toasts(bot)

    refreshed = bot.telegram.last_shown()
    assert UZ["telegram.address.details_default_badge"].strip() in refreshed.text
    assert refreshed.callback_data() == [
        "edit_address_902",
        "delete_address_902",
        "manage_addresses",
    ], "the Set default button survived the address becoming default"


async def test_deleting_an_address_asks_first_and_then_removes_exactly_that_one(
    bot, user, address_book
):
    """Delete is irreversible and the button sits next to Edit. The
    confirmation must name the address being destroyed (title AND full
    address), and the confirm button must carry the SAME id — a mismatch here
    deletes the address the customer was looking at a moment ago.
    """
    await bot.send(user.tap("delete_address_901"))

    confirmation = bot.telegram.last_shown()
    assert confirmation.text == UZ["telegram.address.delete_confirmation"].format(
        title="Uy", address="Bunyodkor 15, Chilonzor"
    )
    assert confirmation.callback_data() == [
        "confirm_delete_address_901",
        "view_address_901",
    ]
    assert deletes(bot) == [], "the confirmation screen must not delete anything"

    await bot.send(user.tap("confirm_delete_address_901"))

    assert deletes(bot) == [901]
    assert set(address_book) == {902}
    assert UZ["telegram.address.deleted_success_toast"] in answered_toasts(bot)
    # And the customer is put back on the list, now showing one address.
    assert bot.telegram.last_shown().callback_data() == [
        "view_address_902",
        "add_new_address",
        "select_edit_address",
        "select_delete_address",
        "menu_profile",
    ]


async def test_a_second_tap_on_yes_delete_reports_the_deletion_it_asked_for(
    bot, user, address_book
):
    """Was a RATCHET; the defect it pinned is fixed. Now the regression guard.

    "Yes, delete" is answered only after the round trip, so the spinner stays
    up and customers tap again. `confirm_delete_address` is a stateless group-0
    handler with no conversation state to protect it, and it used to have no
    idempotency of its own: the second tap issued a second DELETE, the backend
    404d (see `auth.py::delete_user_address`), and the customer was told the
    deletion FAILED for an address that was in fact deleted — so they went
    looking for it, did not find it, and opened a support ticket.

    `callback_dedup` debounces only the first two seconds, so it hid this on an
    impatient double-tap and did nothing for the customer who waits, reads the
    error, and taps again.

    WHAT THE FIX GUARANTEES: a delete of an address that is already gone is the
    outcome the customer asked for. The re-tap still reaches the backend — the
    bot cannot know the row is gone without asking — but a 404 is reported as
    the success it is, and the customer lands back on their address list.
    """
    await bot.send(user.tap("delete_address_901"))
    await bot.send(user.tap("confirm_delete_address_901"))

    assert set(address_book) == {902}
    let_the_dedup_window_lapse()
    await bot.send(user.tap("confirm_delete_address_901"))

    assert deletes(bot) == [901, 901], "the re-tap still asks the backend"
    assert set(address_book) == {902}, "and it destroys nothing else"
    assert UZ["telegram.address.delete_failed_detail"].format(
        error="Address not found"
    ) not in bot.telegram.texts(), (
        "a successful deletion must never be reported as a failure"
    )
    assert answered_toasts(bot).count(UZ["telegram.address.deleted_success_toast"]) == 2
    assert bot.telegram.last_shown().callback_data() == [
        "view_address_902",
        "add_new_address",
        "select_edit_address",
        "select_delete_address",
        "menu_profile",
    ], "the customer is left on their address list, not on an error"


async def test_the_delete_picker_asks_before_it_destroys_anything(
    bot, user, address_book
):
    """The Delete button on the address list opens a PICKER, and picking an
    address there used to delete it on that single tap: the rows carried
    `confirm_delete_address_<id>`, which routes straight past the confirmation
    dialog this module already builds. One mis-tap on a two-row list, and an
    address is gone with no question asked.

    So the picker's rows must land on `delete_address_handler` — the same
    named, quotable confirmation the Delete button inside an address opens.
    """
    await bot.send(user.tap("select_delete_address"))

    picker = bot.telegram.last_shown()
    assert picker.callback_data() == [
        "delete_address_901",
        "delete_address_902",
        "manage_addresses",
    ], "a picker row must not be wired to the confirm handler"

    await bot.send(user.tap("delete_address_901"))

    assert deletes(bot) == [], "picking an address must not delete it"
    assert set(address_book) == {901, 902}
    confirmation = bot.telegram.last_shown()
    assert confirmation.text == UZ["telegram.address.delete_confirmation"].format(
        title="Uy", address="Bunyodkor 15, Chilonzor"
    ), "the customer has to be told WHICH address they are about to destroy"
    assert confirmation.callback_data() == [
        "confirm_delete_address_901",
        "view_address_901",
    ]

    await bot.send(user.tap("confirm_delete_address_901"))

    assert deletes(bot) == [901]
    assert set(address_book) == {902}


async def test_confirming_a_delete_the_backend_refuses_keeps_the_address_and_says_why(
    bot, user, address_book
):
    """A refused delete (row referenced by a live order, for instance) must
    leave the customer somewhere they can act from. The failure toast alone is
    gone in three seconds, so the message body has to carry the reason and a
    Back button to the address that still exists.
    """
    bot.backend.route(
        "DELETE",
        "/api/v1/auth/addresses/901",
        lambda _call: backend_failure("address is used by an active order", 409),
    )

    await bot.send(user.tap("delete_address_901"))
    await bot.send(user.tap("confirm_delete_address_901"))

    assert set(address_book) == {901, 902}, "the address must survive a refused delete"
    failure = bot.telegram.last_shown()
    assert failure.text == UZ["telegram.address.delete_failed_detail"].format(
        error="address is used by an active order"
    )
    assert failure.callback_data() == ["view_address_901"]


async def test_a_stale_delete_button_reports_not_found_instead_of_deleting_something_else(
    bot, user, address_book
):
    """Telegram keeps old messages tappable forever. A customer who deletes an
    address on the web, then scrolls up and taps the bot's old Delete button
    for it, must be told it is gone — not silently walked into a confirmation
    for an id that no longer resolves.
    """
    address_book.pop(902)

    await bot.send(user.tap("delete_address_902"))

    assert UZ["telegram.address.not_found"] in answered_toasts(bot)
    assert bot.telegram.of("editMessageText") == [], (
        "a stale id must not overwrite the message the customer is looking at"
    )
    assert deletes(bot) == []


async def test_renaming_an_address_writes_only_the_title_and_refuses_a_one_letter_name(
    bot, user, address_book
):
    """The rename prompt arms a DB-backed `awaiting_input`, so the customer's
    next plain message is routed by `bot.py`, not by the conversation handler —
    a completely different code path from every other step in this file.

    The PUT must carry the title ALONE: `update_user_address` writes whatever
    keys it is given, so a payload that also echoed stale details would
    overwrite delivery instructions the customer never touched. And a rejected
    one-letter name must leave the prompt armed, or the retry lands in the
    support inbox instead.
    """
    await bot.send(user.tap("edit_address_901"))
    options = bot.telegram.last_shown()
    assert options.callback_data() == [
        "edit_title_901",
        "edit_location_901",
        "edit_details_901",
        "edit_instructions_901",
        "delete_address_901",
        "view_address_901",
    ]

    await bot.send(user.tap("edit_title_901"))
    assert bot.telegram.last_shown().text == UZ[
        "telegram.address.edit_title_prompt"
    ].format(current_title="Uy")

    await bot.send(user.text("O"))
    assert updates(bot) == [], "a one-letter name must not be written"
    assert bot.telegram.last_shown().text == UZ["telegram.address.title_too_short"]
    assert support_posts(bot) == [], (
        "the rejected name leaked into the support inbox, which means the "
        "awaiting_input state was cleared and the retry will leak too"
    )

    await bot.send(user.text("  Ofis  "))

    assert updates(bot) == [(901, {"title": "Ofis"})]
    assert address_book[901]["delivery_instructions"] == "domofon 15"
    assert bot.telegram.last_shown().text == UZ[
        "telegram.address.title_updated_success"
    ].format(title="Ofis")


async def test_backing_out_of_the_rename_stops_the_next_message_being_eaten_as_a_title(
    bot, user, address_book
):
    """The rename prompt's Cancel button is just `view_address_<id>` — there is
    no explicit "clear the pending input" callback. So the disarming has to
    happen inside `view_address`, and if it ever stops happening the customer's
    next unrelated message is silently written over their address title.
    """
    await bot.send(user.tap("edit_title_901"))

    await bot.send(user.tap("view_address_901"))

    await bot.send(user.text("qachon yetkazasiz"))

    assert updates(bot) == [], "an unrelated message was written as the address title"
    assert address_book[901]["title"] == "Uy"
    assert support_posts(bot) == [{"content": "qachon yetkazasiz"}], (
        "with no pending edit the message should reach the support inbox"
    )


async def test_editing_delivery_instructions_writes_only_that_field_and_caps_the_length(
    bot, user, address_book
):
    """Delivery instructions are free text a courier reads at the door, and
    `delivery_instructions` is a Text column with a 200-character bot-side cap.
    Over the cap the bot must refuse and stay armed; under it the PUT must
    carry that key alone so the title and coordinates are untouched.

    The prompt also has to render an address with NO instructions yet without
    printing "None" at the customer.
    """
    await bot.send(user.tap("edit_instructions_902"))

    prompt = bot.telegram.last_shown()
    assert prompt.text == UZ["telegram.address.edit_instructions_prompt"].format(
        current_instructions=UZ["telegram.address.none_value"]
    )
    assert "None" not in prompt.text

    await bot.send(user.text("x" * 201))
    assert updates(bot) == []
    assert bot.telegram.last_shown().text == UZ["telegram.address.instructions_too_long"]

    await bot.send(user.text("Ikkinchi qavat, chap eshik"))

    assert updates(bot) == [
        (902, {"delivery_instructions": "Ikkinchi qavat, chap eshik"})
    ]
    assert address_book[902]["title"] == "Ish"
    assert bot.telegram.last_shown().text == (
        UZ["telegram.address.instructions_updated_intro"]
        + UZ["telegram.address.instructions_new_value"].format(
            value="Ikkinchi qavat, chap eshik"
        )
    )


async def test_a_backend_outage_on_the_address_list_never_shows_the_customer_zero_addresses(
    bot, user, address_book
):
    """`manage_addresses` renders "you have no addresses yet" from an EMPTY
    list. A 500 that is treated as "no addresses" would offer a customer with
    two saved addresses the first-address button — and the obvious next move,
    adding a duplicate, is a real cost. The failure must be a toast over the
    screen they already have.
    """
    bot.backend.route(
        "GET",
        "/api/v1/auth/addresses",
        lambda _call: backend_failure("upstream timeout", status_code=502),
    )

    await bot.send(user.tap("manage_addresses"))

    assert any("upstream timeout" in toast for toast in answered_toasts(bot))
    assert bot.telegram.of("editMessageText") == [], (
        "the customer's screen was replaced despite the backend failing"
    )
    assert UZ["telegram.address.no_addresses"] not in bot.telegram.texts()
    assert UZ["telegram.address.add_first"] not in " ".join(
        label for call in bot.telegram.shown for label in call.button_labels()
    )


async def test_the_delete_picker_says_so_instead_of_opening_an_empty_list(bot, user):
    """`select_delete_address` with nothing to delete must answer with a toast
    and leave the screen alone. Editing the message into an empty picker gives
    the customer a screen whose only button is Back — a dead end they reached
    by tapping a button that should have told them there was nothing there.
    """
    assert bot.backend.addresses == {}

    await bot.send(user.tap("select_delete_address"))

    assert UZ["telegram.address.no_addresses_to_delete"] in answered_toasts(bot)
    assert bot.telegram.of("editMessageText") == [], (
        "the customer was dropped onto a picker whose only button is Back"
    )


async def test_the_unimplemented_edit_buttons_explain_themselves_instead_of_hanging(
    bot, user, address_book
):
    """Two of the six buttons on the edit menu — Location and Details — have no
    implementation behind them. A callback that runs no code leaves Telegram's
    loading spinner up until it times out, which reads as a frozen bot and is
    the exact symptom `callback_dedup` was written to chase.

    So the contract for an unimplemented button is: answer the tap with an
    explanation, and leave the menu on screen so the customer can pick one of
    the four buttons that DO work.
    """
    await bot.send(user.tap("edit_address_901"))
    bot.telegram.reset()

    await bot.send(user.tap("edit_location_901"))
    assert UZ["telegram.address.location_edit_not_supported"] in answered_toasts(bot)

    await bot.send(user.tap("edit_details_901"))
    assert UZ["telegram.address.details_edit_coming_soon"] in answered_toasts(bot)

    assert bot.telegram.of("editMessageText") == [], (
        "an unimplemented button replaced the menu the customer still needs"
    )
    assert updates(bot) == [] and deletes(bot) == []
