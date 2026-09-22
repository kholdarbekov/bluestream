"""A sales agent photographs the shop, at whatever step they are on.

Driven through the real staff ``Application`` (tests/staff_bot/ptb_harness.py),
because the whole point of the feature is WHERE it is registered: a photo
handler wired into one state is a camera that works on one screen, and the
bug is invisible to a handler-level test.

Every verdict on the picture is the backend's. ``is_duplicate`` is its
SHA-256 answer over the original bytes, the size ceiling is the storage
service's, and the only thing this bot refuses on its own is a FORWARD --
which is not a judgement about the file, but about where it was taken.
"""

import pytest

from staff_bot.handlers.sales.visit import (
    FLOW_KEY,
    V_CHECKIN,
    V_CLOSE,
    V_CONFIRM,
    V_DAY,
    V_NOTES,
    V_ORDER,
    V_ORDER_EDIT,
    V_PAYMENT,
    V_STOCK,
    V_STOCK_QTY,
)
from staff_bot.keyboards.sales import PHOTO_KINDS
from tests.bot_dispatcher_harness import DEFAULT_FILE_BYTES
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_sales_hub_journey import _agent, _calls, _curated
from tests.staff_bot.test_sales_visit_journey import CONV, SALES, VISIT_ID, _start, _to_stock
from tests.staff_bot.test_sales_visit_orders_journey import _to_order

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PHOTOS = f"{SALES}/visits/{VISIT_ID}/photos"
EVERY_VISIT_STATE = (
    V_CHECKIN, V_STOCK, V_STOCK_QTY, V_ORDER, V_ORDER_EDIT,
    V_PAYMENT, V_DAY, V_NOTES, V_CONFIRM, V_CLOSE,
)


def _user_data(harness):
    """The agent's own `context.user_data`, where the visit draft lives.

    `tests/staff_bot/test_sales_visit_journey.py` has no accessor of its own
    (it reaches through `harness.application.user_data[...]` at each call
    site), so the one this file needs twice is written here rather than added
    to a module three other files import fixtures from.
    """
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


async def _to_notes(harness, ops):
    """Walk to the delivery-notes step, the one text state inside the order.

    The same taps `tests/staff_bot/test_sales_visit_orders_journey.py` uses to
    get there -- a second walker would be a second definition of "how an agent
    reaches the notes screen".
    """
    await _to_order(harness, ops)
    await harness.send(ops.tap("staff_sales_v_ordersugg"))
    await harness.send(ops.tap("staff_sales_v_day_tomorrow"))


def _photo_reply(photo_id=4, duplicate=False):
    """`serialize_visit_photo` -- what POST /photos answers with."""
    return {"photo": {
        "id": photo_id, "visit_id": VISIT_ID, "kind": "storefront",
        "file_path": f"sales_visits/{photo_id}.jpg",
        "sha256": "a" * 64, "telegram_file_unique_id": "u-l",
        "received_at": "2026-09-15T05:20:00+00:00",
        "duplicate_of_photo_id": 2 if duplicate else None,
        "is_duplicate": duplicate,
    }}


async def test_a_photo_asks_what_it_is_and_then_posts_the_bytes(monkeypatch):
    """One picture, one question, one multipart POST -- and the step is untouched.

    The agent is counting the shelf. Sending a photo must not move them off
    that screen, which is why every photo path returns None: PTB reads that
    as "state unchanged", and the shelf grid one message up stays live.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    await harness.send(ops.photo())

    picker = harness.telegram.last_shown()
    assert _curated("staff.sales.visit.photo_kind_prompt") in picker.text
    assert picker.callback_data() == [f"staff_sales_v_photo_{kind}" for kind in PHOTO_KINDS]
    assert _calls(harness, "POST", PHOTOS) == []
    assert harness.conversation_state(CONV) == V_STOCK

    await harness.send(ops.tap("staff_sales_v_photo_storefront"))

    posted = _calls(harness, "POST", PHOTOS)
    assert [call.data for call in posted] == [
        {"kind": "storefront", "telegram_file_unique_id": "u-l"}
    ]
    # The FILENAME carries `.jpg`: `FileStorageService._validate_file` checks
    # the extension, and a Telegram download has no name of its own.
    assert [call.files for call in posted] == [
        {"file": (f"visit_{VISIT_ID}_u-l.jpg", DEFAULT_FILE_BYTES, "image/jpeg")}
    ]
    assert _curated("staff.sales.visit.photo_saved") in harness.telegram.last_shown().text
    assert harness.telegram.last_shown().callback_data() == []
    assert harness.conversation_state(CONV) == V_STOCK


async def test_the_duplicate_line_is_the_backends_verdict_not_a_second_upload(monkeypatch):
    """`is_duplicate` is a field, so the bot hashes nothing and posts once.

    The duplicate is still STORED (the exception feed is what surfaces it in
    phase 3); the agent is told, not refused.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply(duplicate=True))

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_shelf"))

    assert len(_calls(harness, "POST", PHOTOS)) == 1
    assert _calls(harness, "POST", PHOTOS)[0].data["kind"] == "shelf"
    shown = harness.telegram.last_shown().text
    assert _curated("staff.sales.visit.photo_saved") in shown
    assert _curated("staff.sales.visit.photo_duplicate") in shown


async def test_a_forwarded_photo_is_refused_before_anything_is_downloaded(monkeypatch):
    """Refused client-side, and refused EARLY.

    A forward is a picture of someone else's shop, and a hash can only catch
    a repeat -- never a photo that was never of this outlet. Downloading it
    first would put the bytes on the wire anyway for a picture that is about
    to be thrown away.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    await harness.send(ops.photo(forwarded=True))

    assert _calls(harness, "POST", PHOTOS) == []
    assert harness.telegram.of("getFile") == []
    answered = [c.params.get("text", "") for c in harness.telegram.of("answerCallbackQuery")]
    replies = [c.text for c in harness.telegram.shown]
    assert any(
        _curated("staff.sales.visit.photo_forwarded") in text
        for text in answered + replies
    )
    assert harness.conversation_state(CONV) == V_STOCK


async def test_a_burst_draws_one_picker_and_one_tap_files_all_of_them(monkeypatch):
    """Three shelf photos in a row are one gesture, not three questions.

    Telegram delivers a media group as separate updates. A picker per photo
    would leave two stale keyboards behind and -- since only one draft slot
    exists -- file the last picture three times.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    for index in (1, 2, 3):
        await harness.send(ops.photo(file_id=f"shot-{index}", file_unique_id=f"u-{index}"))

    # Three photos, three DOWNLOADS: the queue holds three sets of bytes, not
    # one picture re-read three times.
    assert len(set(harness.telegram.downloaded)) == 3, harness.telegram.downloaded
    pickers = [
        call for call in harness.telegram.shown
        if _curated("staff.sales.visit.photo_kind_prompt") in call.text
    ]
    assert len(pickers) == 1

    await harness.send(ops.tap("staff_sales_v_photo_shelf"))

    posted = _calls(harness, "POST", PHOTOS)
    assert [call.data["telegram_file_unique_id"] for call in posted] == ["u-1", "u-2", "u-3"]
    assert [call.files["file"][0] for call in posted] == [
        f"visit_{VISIT_ID}_u-1.jpg", f"visit_{VISIT_ID}_u-2.jpg", f"visit_{VISIT_ID}_u-3.jpg"
    ]
    assert {call.data["kind"] for call in posted} == {"shelf"}


async def test_a_refused_file_shows_the_backends_own_sentence_and_is_not_retried(monkeypatch):
    """SALES_PHOTO_INVALID lands as the mapped copy, and the queue is emptied.

    A retry button on a file the backend has already refused re-posts the
    same bytes for ever. The queue is drained BEFORE the posts, so the
    second tap on the same picker has nothing to send.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route(
        "POST", PHOTOS,
        lambda _c: staff_backend_failure("Bad photo", 400, "SALES_PHOTO_INVALID"),
    )

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_other"))

    assert len(_calls(harness, "POST", PHOTOS)) == 1
    assert _curated("staff.sales.error.photo_invalid") in harness.telegram.last_shown().text
    assert _curated("staff.sales.visit.photo_saved") not in harness.telegram.last_shown().text

    await harness.send(ops.tap("staff_sales_v_photo_other"))
    assert len(_calls(harness, "POST", PHOTOS)) == 1
    answered = [c.params.get("text", "") for c in harness.telegram.of("answerCallbackQuery")]
    assert any(_curated("staff.sales.visit.photo_failed") in text for text in answered)


async def test_an_oversize_photo_reads_like_any_other_unusable_file(monkeypatch):
    """A 413 is the ONE photo refusal with no error code to map.

    `MAX_CONTENT_LENGTH` is Flask's, enforced before the route function runs
    (`business_app/config/base.py`), so the body is HTML and the client's
    catch-all branch returns `error_code=None` with `status_code=413`. Left
    to `_resolve_api_error_message` that is the generic "Something went
    wrong" -- for the one failure whose remedy the agent can carry out on the
    spot. The verdict is the same as `SALES_PHOTO_INVALID`'s (this file
    cannot be stored), so the sentence is the same too, and this test is what
    stops the two drifting apart.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route(
        "POST", PHOTOS,
        lambda _c: staff_backend_failure("Request Entity Too Large", 413),
    )

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_other"))

    assert len(_calls(harness, "POST", PHOTOS)) == 1
    shown = harness.telegram.last_shown().text
    assert _curated("staff.sales.error.photo_invalid") in shown
    assert _curated("staff.sales.visit.photo_saved") not in shown


async def test_a_photo_taken_before_the_check_in_is_filed_on_the_check_in_screen(monkeypatch):
    """The first thing an agent photographs is the storefront, at the door.

    Driven on a DIFFERENT state from every other case here, because "at
    every step" is the claim and V_CHECKIN is the step where the screen is a
    reply keyboard rather than an inline one.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _start(harness, ops)
    assert harness.conversation_state(CONV) == V_CHECKIN
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_storefront"))

    assert [call.data["kind"] for call in _calls(harness, "POST", PHOTOS)] == ["storefront"]
    assert harness.conversation_state(CONV) == V_CHECKIN


async def test_a_caption_is_not_a_visit_note(monkeypatch):
    """The one state where `filters.PHOTO` sits beside a text handler that WRITES.

    In V_NOTES the text the agent types IS the order's delivery note.
    `filters.PHOTO` matches a captioned photo there too, so a captioned photo
    has to mean one thing, and the ruling is: it is a photo, and the caption
    is discarded. Driven rather than asserted in a docstring, because the
    alternative failure is silent -- an agent typing under a picture would
    either lose the words with no sign, or have them filed as a note they
    never saw a confirmation for. If a later phase gives captions a
    destination, this test is what must change first.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_notes(harness, ops)
    assert harness.conversation_state(CONV) == V_NOTES
    harness.backend.route("POST", PHOTOS, lambda _c: _photo_reply())

    await harness.send(ops.photo(caption="peshtoq yangi, egasi rozi"))
    await harness.send(ops.tap("staff_sales_v_photo_storefront"))

    # The photo was filed, and nothing but the photo went up the wire.
    posted = _calls(harness, "POST", PHOTOS)
    assert [call.data["kind"] for call in posted] == ["storefront"]
    assert "caption" not in (posted[0].data or {})
    assert "notes" not in (posted[0].data or {})
    # The note is still unwritten and the agent is still on the notes step.
    assert harness.conversation_state(CONV) == V_NOTES
    assert _user_data(harness)[FLOW_KEY].get("delivery_notes") in (None, "")
    # And the caption is not quietly echoed back as though it had been kept.
    assert "peshtoq yangi" not in harness.telegram.last_shown().text


async def test_every_visit_state_answers_both_a_photo_and_the_kind_tap(monkeypatch):
    """The wiring claim, checked against the registration PTB actually uses.

    One end-to-end drive can only prove the state it runs in. This walks the
    ten states of the visit conversation and asks each one what it would do
    with a photo and with a kind tap -- the question a handler-level test
    cannot ask at all, and the one a missing line in `bot.py` answers with
    silence on a screen the agent is standing on.
    """
    harness, ops, _ = await _agent(monkeypatch)
    conversation = harness.conversation(CONV)

    missing = []
    for state in EVERY_VISIT_STATE:
        handlers = conversation.states[state]
        claims_photo = [
            handler.callback.__name__ for handler in handlers
            if handler.check_update(ops.photo()) not in (None, False)
        ]
        claims_tap = [
            handler.callback.__name__ for handler in handlers
            if handler.check_update(ops.tap("staff_sales_v_photo_shelf")) not in (None, False)
        ]
        if claims_photo != ["receive_photo"] or claims_tap != ["choose_photo_kind"]:
            missing.append((state, claims_photo, claims_tap))

    assert not missing, f"states that do not answer a photo: {missing}"


async def test_a_photo_posted_to_a_visit_that_closed_under_the_agent_ends_the_visit(monkeypatch):
    """409 `SALES_VISIT_NOT_OPEN` means the same thing here as on every other write.

    `abandon_stale_visits` runs hourly and a second device can close a visit too,
    so the photo POST is one of the four places the backend can answer "that
    visit is over". The order and close paths END the conversation on it; the
    photo path used to read the verdict and stay, leaving the agent on a screen
    whose every button posts to a dead id with no draft behind it.
    """
    harness, ops, _ = await _agent(monkeypatch)
    await _to_stock(harness, ops)
    harness.backend.route(
        "POST", PHOTOS,
        lambda _c: staff_backend_failure("visit closed", 409, "SALES_VISIT_NOT_OPEN"),
    )

    await harness.send(ops.photo())
    await harness.send(ops.tap("staff_sales_v_photo_shelf"))

    assert harness.conversation_state(CONV) is None
    assert FLOW_KEY not in _user_data(harness)
    # The same lines the other three photo verdicts render -- the agent is told
    # why, on the screen they are looking at.
    assert _curated("staff.sales.error.visit_not_open") in harness.telegram.last_shown().text
