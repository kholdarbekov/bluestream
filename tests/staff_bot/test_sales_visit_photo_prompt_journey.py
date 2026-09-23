"""D26: after check-in at a shop or an office, the bot asks ONCE for a photo.

Driven through the real staff `Application` (tests/staff_bot/ptb_harness.py): the prompt is a
conversation STATE, and whether it exists, what it answers and where it leads are all wiring a
handler-level test cannot see.
"""
import hashlib

import pytest

from staff_bot.handlers.sales.visit import FLOW_KEY, V_PHOTO, V_STOCK
from tests.bot_dispatcher_harness import DEFAULT_FILE_BYTES
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_CHAT_ID, DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import _agent, _calls, _curated
from tests.staff_bot.test_sales_visit_journey import (
    ABANDON, ACCURACY, CHECKIN, CONV, CURRENT, DOOR, PRODUCT_ITEMS, PRODUCTS, SALES, STOCK_CHECK, VISIT_ID,
    WATER_ID, _outlet_card, _start, _visit,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PHOTOS = f"{SALES}/visits/{VISIT_ID}/photos"
DEFAULT_SHA = hashlib.sha256(DEFAULT_FILE_BYTES).hexdigest()


def _photo_reply(photo_id=4):
    return {"photo": {
        "id": photo_id, "visit_id": VISIT_ID, "kind": "storefront", "sha256": DEFAULT_SHA,
        "telegram_file_unique_id": "u-l", "received_at": "2026-09-23T05:20:00+00:00",
        "duplicate_of_photo_id": None, "is_duplicate": False,
    }}


def _sent_ids(harness, monkeypatch):
    """(text, message_id) of every message the bot SENDS, as the fake Telegram numbered it.

    `TelegramCall` records what the bot asked for, not what Telegram answered, and the prompt's
    id exists only in the answer -- which is the id the bot has to find the prompt by later.
    """
    sent = []
    real = harness.telegram._result_for

    def spy(endpoint, params):
        result = real(endpoint, params)
        if endpoint == "sendMessage":
            sent.append((params.get("text", ""), result["message_id"]))
        return result

    monkeypatch.setattr(harness.telegram, "_result_for", spy)
    return sent


async def _checked_in(harness, ops, *, photo_requested=True):
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda _c: {"visit": _visit(
        current_step="stock", checkin_skipped=True, photo_requested=photo_requested,
    )})
    await harness.send(ops.tap("staff_sales_v_skipcheckin"))


async def test_a_shop_check_in_asks_for_one_photo_then_draws_the_shelf(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)

    prompt = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.photo_request") in prompt.text
    assert prompt.callback_data() == ["staff_sales_v_photoskip", "staff_sales_v_abandon"]
    assert harness.conversation_state(CONV) == V_PHOTO

    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())
    await harness.send(ops.photo())
    assert _curated("staff.sales.visit.photo_kind_prompt") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_PHOTO

    await harness.send(ops.tap("staff_sales_v_photo_storefront"))

    # Telegram's reference and the digest of the bytes -- never the bytes themselves (D27).
    assert [call.data for call in _calls(harness, "POST", PHOTOS)] == [{
        "kind": "storefront", "telegram_file_id": "photo-file-id",
        "telegram_file_unique_id": "u-l", "sha256": DEFAULT_SHA,
    }]
    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.stock_title") in harness.telegram.last_shown().text


async def test_a_photo_that_answers_the_prompt_takes_the_prompts_buttons_away(monkeypatch):
    """The prompt is a message of its own and the shelf arrives as a NEW one, so nothing replaces
    it. Left live, its Skip would be tapped from a later step -- the quantity grid, the basket --
    where no handler claims it: `stale_tap` re-seeds the draft from the server and the counts or
    the basket not yet sent are gone. So the keyboard is cleared at its source."""
    harness, ops, _ = await _agent(monkeypatch)
    sent = _sent_ids(harness, monkeypatch)
    await _checked_in(harness, ops)
    prompt_id = next(mid for text, mid in sent if _curated("staff.sales.visit.photo_request") in text)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_storefront"))

    cleared = [
        call for call in harness.telegram.of("editMessageReplyMarkup")
        if int(call.params.get("message_id", 0)) == prompt_id
    ]
    assert len(cleared) == 1, harness.telegram.of("editMessageReplyMarkup")
    assert int(cleared[0].params["chat_id"]) == DEFAULT_DRIVER_CHAT_ID
    assert cleared[0].reply_markup == {}
    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.stock_title") in harness.telegram.last_shown().text


async def test_a_pin_check_in_asks_too(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _start(harness, ops)
    harness.backend.route("POST", CHECKIN, lambda _c: {"visit": _visit(
        current_step="stock", checkin_latitude=DOOR[0], checkin_longitude=DOOR[1],
        checkin_accuracy_m=ACCURACY, distance_m=12.0, in_radius=True, photo_requested=True,
    )})

    await harness.send(ops.location(*DOOR, horizontal_accuracy=ACCURACY))

    assert harness.conversation_state(CONV) == V_PHOTO
    assert _curated("staff.sales.visit.photo_request") in harness.telegram.last_shown().text


async def test_skip_goes_straight_to_the_shelf_and_posts_nothing(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)

    await harness.send(ops.tap("staff_sales_v_photoskip"))

    assert _calls(harness, "POST", PHOTOS) == []
    assert harness.conversation_state(CONV) == V_STOCK
    assert _curated("staff.sales.visit.stock_title") in harness.telegram.last_shown().text


async def test_a_home_visit_is_never_asked(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops, photo_requested=False)

    shown = " ".join(call.text for call in harness.telegram.shown)
    assert _curated("staff.sales.visit.photo_request") not in shown
    assert harness.conversation_state(CONV) == V_STOCK


async def test_resuming_lands_on_the_shelf_and_does_not_ask_again(monkeypatch):
    """The prompt is bot-local: the server's step is already `stock`. A restart must not ask twice."""
    harness, ops, _ = await _agent(monkeypatch)
    harness.backend.route("GET", CURRENT, lambda _c: {
        "visit": _visit(current_step="stock", checkin_skipped=True, photo_requested=True),
        "outlet": _outlet_card(),
    })
    harness.backend.route("GET", PRODUCTS, lambda _c: {"items": PRODUCT_ITEMS})

    await harness.send(ops.tap("staff_sales_visit_resume"))

    assert harness.conversation_state(CONV) == V_STOCK
    shown = " ".join(call.text for call in harness.telegram.shown)
    assert _curated("staff.sales.visit.photo_request") not in shown


async def test_the_prompts_leftover_skip_on_the_shelf_keeps_the_counts(monkeypatch):
    """The backstop: a photo answered the prompt but Telegram refused to clear its keyboard, so its
    Skip is still on screen above the shelf. A tap on it must not reach `stale_tap`, which re-reads
    the visit and drops counts not yet sent."""
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())
    await harness.send(ops.photo())
    harness.telegram.fail("editMessageReplyMarkup", "Bad Request: message to edit not found")
    await harness.send(ops.tap("staff_sales_v_photo_shelf"))
    harness.telegram.clear_failures()
    assert harness.conversation_state(CONV) == V_STOCK
    # A count the agent has entered and not sent: it reaches the server only with the shelf's Done.
    await harness.send(ops.tap(f"staff_sales_v_stock_{WATER_ID}"))
    await harness.send(ops.tap(f"staff_sales_v_qty_{WATER_ID}_3"))
    await harness.send(ops.tap("staff_sales_v_stockback"))
    assert harness.conversation_state(CONV) == V_STOCK
    reads_before = len(_calls(harness, "GET", CURRENT))
    clears_before = len(harness.telegram.of("editMessageReplyMarkup"))

    await harness.send(ops.tap("staff_sales_v_photoskip"))

    assert harness.conversation_state(CONV) == V_STOCK
    assert len(_calls(harness, "GET", CURRENT)) == reads_before
    assert _calls(harness, "POST", STOCK_CHECK) == []
    draft = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID][FLOW_KEY]
    assert draft["stock"][str(WATER_ID)]["on_hand"] == 3
    # The tap cleared its own keyboard and did nothing else.
    assert len(harness.telegram.of("editMessageReplyMarkup")) == clears_before + 1


async def test_a_refused_photo_keeps_the_prompt_and_skip_still_works(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: staff_backend_failure("Bad photo", 400, "SALES_PHOTO_INVALID"))

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_other"))

    assert _curated("staff.sales.error.photo_invalid") in harness.telegram.last_shown().text
    assert harness.conversation_state(CONV) == V_PHOTO
    await harness.send(ops.tap("staff_sales_v_photoskip"))
    assert harness.conversation_state(CONV) == V_STOCK


async def test_a_burst_on_the_prompt_files_every_photo_and_draws_the_shelf_once(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    for index in (1, 2, 3):
        await harness.send(ops.photo(file_id=f"shot-{index}", file_unique_id=f"u-{index}"))
    await harness.send(ops.tap("staff_sales_v_photo_shelf"))

    assert [c.data["telegram_file_id"] for c in _calls(harness, "POST", PHOTOS)] == ["shot-1", "shot-2", "shot-3"]
    shelves = [c for c in harness.telegram.shown if _curated("staff.sales.visit.stock_title") in c.text]
    assert len(shelves) == 1
    assert harness.conversation_state(CONV) == V_STOCK


async def test_a_file_sent_instead_of_a_photo_leaves_the_prompt_standing(monkeypatch):
    """An uncompressed image arrives as a DOCUMENT, which no photo handler matches."""
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)

    await harness.send(ops.document(file_name="shop.jpg", mime_type="image/jpeg"))

    assert _calls(harness, "POST", PHOTOS) == []
    assert harness.conversation_state(CONV) == V_PHOTO
    await harness.send(ops.tap("staff_sales_v_photoskip"))
    assert harness.conversation_state(CONV) == V_STOCK


async def test_abandon_on_the_prompt_abandons_the_visit(monkeypatch):
    harness, ops, _ = await _agent(monkeypatch)
    await _checked_in(harness, ops)
    harness.backend.route("POST", ABANDON, lambda _c: {"visit": _visit(
        status="abandoned", ended_at="2026-09-23T06:00:00+00:00",
    )})

    await harness.send(ops.tap("staff_sales_v_abandon"))

    assert [call.data for call in _calls(harness, "POST", ABANDON)] == [{}]
    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
