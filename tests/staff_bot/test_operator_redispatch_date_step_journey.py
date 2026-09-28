"""An operator gives a failed delivery its new date, through the real staff dispatcher.

F9 and F12 of docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md.
The date step is stateless. Every tap that draws dates asks the backend for that one
delivery's bounds (`GET /api/v1/staff/delivery/failed/<id>`), and a date tap posts the day
on its button and nothing else. The backend here publishes a `reschedule_min_date` years
away from the bot's own clock, so any date the bot worked out for itself would show up as
a wrong button or a wrong POST body (Review Focus 1).

Only the backend (`api_client._make_request`) and Telegram are faked. The assertions are
the bytes that reached each of them.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from shared.constants import DISPLAY_TIMEZONE
from staff_bot.api_client import api_client
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    LANGUAGES,
    _SEED,
    _curated,
    _translation_table,
    alerts,
    backend_calls,
    build_staff,
    capture_errors,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FAILED_LIST = "/api/v1/staff/delivery/failed"
DELIVERY_ID = 9001
FAILED_ONE = f"{FAILED_LIST}/{DELIVERY_ID}"
REDISPATCH = f"/api/v1/staff/delivery/redispatch/{DELIVERY_ID}"

# The bounds the backend publishes for this delivery: its local today and the last
# bookable day. Years from the bot's clock on purpose (see the module docstring).
PUBLISHED_MIN = "2031-03-14"
PUBLISHED_MAX = "2031-03-29"
# A held re-date's release instant as the route publishes it: 08:00 in Tashkent.
HELD_RELEASE_AT = "2031-03-22T03:00:00+00:00"

# Every string these journeys compare against, read from the seed that ships it.
REDISPATCH_KEYS = (
    "staff.redispatch.title",
    "staff.redispatch.pick",
    "staff.redispatch.button",
    "staff.redispatch.attempts",
    "staff.redispatch.showing",
    "staff.redispatch.driver",
    "staff.redispatch.failed_at",
    "staff.redispatch.all_failed",
    "staff.redispatch.today",
    "staff.redispatch.tomorrow",
    "staff.redispatch.pick_day",
    "staff.redispatch.pick_day_prompt",
    "staff.redispatch.back",
    "staff.redispatch.success",
    "staff.redispatch.success_held",
    "staff.redispatch.success_in_pool",
    "staff.redispatch.customer_notified_telegram",
    "staff.redispatch.customer_notified_email",
    "staff.redispatch.customer_unreachable",
    "staff.delivery.reason.customer_unavailable",
    "staff.error.api.not_redispatchable",
    "staff.error.api.reschedule_date_in_past",
)


def _table() -> dict:
    """The operator journey's copy plus this flow's, from the seed that ships it.

    A key the seed has no value for is left out here. The test that reads it then
    fails in `_copy`, naming the key, instead of every fixture failing at once.
    """
    overrides = {}
    for key in REDISPATCH_KEYS:
        for language in LANGUAGES:
            value = _SEED._curated_value(key, language)
            if value is not None:
                overrides[(language, key)] = value
    return _translation_table(overrides)


def _copy(key: str) -> str:
    return _curated(key, "en")


def _row(delivery_id: int = DELIVERY_ID, **overrides) -> dict:
    """One failed row, shaped as `api/staff.py::_failed_delivery_row` publishes it."""
    row = {
        "delivery_id": delivery_id,
        "order_id": delivery_id + 100_000,
        "order_number": f"TG_{delivery_id:06d}_31",
        "status": "failed",
        "customer_name": "Dilnoza Rahimova",
        "customer_phone": "+998901112233",
        "address": "Chilonzor 9-kvartal, 14-uy",
        "total_amount": 45000.0,
        "failed_delivery_reason": "customer_unavailable",
        "delivery_attempts": 1,
        "driver_name": "Bobur Toshmatov",
        "failed_at": "2031-03-13T09:05:00+00:00",
        "reschedule_min_date": PUBLISHED_MIN,
        "reschedule_max_date": PUBLISHED_MAX,
    }
    row.update(overrides)
    return row


def _landed(delivery_date: str, **overrides) -> dict:
    """The redispatch route's success body."""
    body = {
        "delivery_id": DELIVERY_ID,
        "status": "rescheduled",
        "message": "Delivery rescheduled",
        "release_at": None,
        "delivery_date": delivery_date,
        "notifies_customer": False,
        "customer_channel": None,
    }
    body.update(overrides)
    return body


def _keyboards(harness) -> list:
    """Every keyboard-only edit, in order: what the card's buttons became."""
    return harness.telegram.of("editMessageReplyMarkup")


def _assert_the_card_stayed_and_lost_its_buttons(harness):
    """A re-date that went through leaves the card's text alone, so it still names the
    order and the customer the success reply below it is about. Only its buttons go:
    the delivery has left the failed list, so none of them can work any more."""
    assert harness.telegram.of("editMessageText") == [], "the card must keep naming the order"
    assert [edit.callback_data() for edit in _keyboards(harness)] == [[]], (
        "the card's buttons must be taken away, once"
    )


@pytest.fixture
async def operator(monkeypatch):
    harness = await build_staff(monkeypatch, roles=["operator"], translations=_table())
    harness.errors = capture_errors(harness)
    harness.backend.route("GET", FAILED_ONE, lambda _call: {"delivery": _row()})
    return harness


# ---------------------------------------------------------------------------
# The date step
# ---------------------------------------------------------------------------


async def test_today_on_an_alert_card_posts_the_min_date_the_backend_published(operator):
    """Review Focus 1. "Today" is the backend's `reschedule_min_date`, fetched for this
    delivery on the tap, and the POST carries exactly that day."""
    assert date.fromisoformat(PUBLISHED_MIN) != datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).date(), (
        "the published min must differ from the bot's clock, or this test proves nothing"
    )
    ops, _labels = await sign_in(operator)
    operator.backend.route("POST", REDISPATCH, lambda _call: _landed(PUBLISHED_MIN, status="scheduled"))

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))

    assert [(call.method, call.endpoint) for call in operator.backend.calls] == [("GET", FAILED_ONE)], (
        "the date step must ask the backend for this delivery's bounds, and only that"
    )
    assert operator.telegram.of("editMessageText") == [], "the date step changes the keyboard, not the card"
    step = _keyboards(operator)[-1]
    assert step.callback_data() == [
        f"staff_redispatch_on_{DELIVERY_ID}_20310314",
        f"staff_redispatch_on_{DELIVERY_ID}_20310315",
        f"staff_redispatch_pick_{DELIVERY_ID}_1",
        f"staff_redispatch_back_{DELIVERY_ID}_1",
    ]
    assert step.button_labels() == [
        f"{_copy('staff.redispatch.today')} · 14.03",
        f"{_copy('staff.redispatch.tomorrow')} · 15.03",
        f"🗓 {_copy('staff.redispatch.pick_day')}",
        f"⬅️ {_copy('staff.redispatch.back')}",
    ]

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310314"))

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [
        {"delivery_date": PUBLISHED_MIN}
    ]
    assert not operator.errors


async def test_a_day_picked_from_the_grid_posts_exactly_that_day(operator):
    """Pick a day re-reads the bounds, draws every allowed day three to a row, and the
    day tapped is the day posted."""
    ops, _labels = await sign_in(operator)
    operator.backend.route("POST", REDISPATCH, lambda _call: _landed("2031-03-22"))

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_0"))
    await operator.send(ops.tap(f"staff_redispatch_pick_{DELIVERY_ID}_0"))

    assert len(backend_calls(operator, "GET", FAILED_ONE)) == 2, (
        "the grid must ask for the bounds again, not trust the step it came from"
    )
    assert _copy("staff.redispatch.pick_day_prompt") in alerts(operator)
    grid = _keyboards(operator)[-1]
    rows = grid.reply_markup["inline_keyboard"]
    # 14.03 … 29.03 is sixteen days: five rows of three, one of one, then Back.
    assert [len(row) for row in rows] == [3, 3, 3, 3, 3, 1, 1]
    assert [button["text"] for button in rows[0]] == ["14.03", "15.03", "16.03"]
    assert grid.callback_data()[-2:] == [
        f"staff_redispatch_on_{DELIVERY_ID}_20310329",
        f"staff_redispatch_do_{DELIVERY_ID}_0",
    ]
    assert rows[-1][0]["text"] == f"⬅️ {_copy('staff.redispatch.back')}"

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310322"))

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [
        {"delivery_date": "2031-03-22"}
    ]
    assert not operator.errors


async def test_tomorrow_is_left_out_when_the_backend_allows_one_day(operator):
    """A grocery contract ending on the published min cuts the bounds to that one day
    (spec §3.5), so neither Tomorrow nor a second grid day may be offered."""
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "GET", FAILED_ONE, lambda _call: {"delivery": _row(reschedule_max_date=PUBLISHED_MIN)}
    )

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_0"))

    assert _keyboards(operator)[-1].callback_data() == [
        f"staff_redispatch_on_{DELIVERY_ID}_20310314",
        f"staff_redispatch_pick_{DELIVERY_ID}_0",
        f"staff_redispatch_back_{DELIVERY_ID}_0",
    ]

    await operator.send(ops.tap(f"staff_redispatch_pick_{DELIVERY_ID}_0"))

    assert _keyboards(operator)[-1].callback_data() == [
        f"staff_redispatch_on_{DELIVERY_ID}_20310314",
        f"staff_redispatch_do_{DELIVERY_ID}_0",
    ]


async def test_every_date_drawing_tap_refetches_so_the_day_turning_moves_today(operator):
    """Review Focus 1, the stale half. The date step was drawn before local midnight and
    the operator comes back to it after. Back redraws the card without asking the
    backend, and the next Pick a new date asks again, so Today is the new day."""
    ops, _labels = await sign_in(operator)
    published = {"min": PUBLISHED_MIN}
    operator.backend.route(
        "GET", FAILED_ONE, lambda _call: {"delivery": _row(reschedule_min_date=published["min"])}
    )
    operator.backend.route("POST", REDISPATCH, lambda _call: _landed("2031-03-15", status="scheduled"))

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))
    assert _keyboards(operator)[-1].callback_data()[0] == f"staff_redispatch_on_{DELIVERY_ID}_20310314"

    published["min"] = "2031-03-15"  # the backend's local day turned
    await operator.send(ops.tap(f"staff_redispatch_back_{DELIVERY_ID}_1"))
    assert len(backend_calls(operator, "GET", FAILED_ONE)) == 1, "Back must redraw the card without a fetch"

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))
    today_button = _keyboards(operator)[-1].callback_data()[0]
    assert today_button == f"staff_redispatch_on_{DELIVERY_ID}_20310315"

    await operator.send(ops.tap(today_button))

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [
        {"delivery_date": "2031-03-15"}
    ]
    assert not operator.errors


@pytest.mark.parametrize(
    "origin, card_buttons",
    [
        pytest.param(1, [f"staff_redispatch_do_{DELIVERY_ID}_1", "staff_redispatch_failed_new"], id="alert-card"),
        pytest.param(0, [f"staff_redispatch_do_{DELIVERY_ID}_0"], id="list-card"),
    ],
)
async def test_back_restores_the_card_it_came_from_without_a_fetch(operator, origin, card_buttons):
    ops, _labels = await sign_in(operator)

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_{origin}"))
    await operator.send(ops.tap(f"staff_redispatch_back_{DELIVERY_ID}_{origin}"))

    restored = _keyboards(operator)[-1]
    assert restored.callback_data() == card_buttons
    assert restored.button_labels()[0] == f"📅 {_copy('staff.redispatch.button')}"
    assert len(backend_calls(operator, "GET", FAILED_ONE)) == 1
    assert operator.telegram.of("editMessageText") == []


async def test_a_list_card_drawn_before_the_date_step_existed_still_opens_it(operator):
    """Rolling safety (spec §9). An old list card sends a bare `staff_redispatch_do_<id>`,
    which now opens the date step as a list card. It no longer re-dates on its own."""
    ops, _labels = await sign_in(operator)

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}"))

    assert _keyboards(operator)[-1].callback_data()[-2:] == [
        f"staff_redispatch_pick_{DELIVERY_ID}_0",
        f"staff_redispatch_back_{DELIVERY_ID}_0",
    ]
    assert backend_calls(operator, "POST", REDISPATCH) == []


async def test_a_driver_cannot_open_the_date_step(monkeypatch):
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], translations=_table())
    staff_member, _labels = await sign_in(harness)

    await harness.send(staff_member.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))

    assert backend_calls(harness, "GET", FAILED_ONE) == []
    assert alerts(harness) == [_copy("staff.unauthorized")]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_a_date_step_on_a_delivery_already_re_dated_shows_the_refusal_and_no_dates(operator):
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "GET",
        FAILED_ONE,
        lambda _call: staff_backend_failure(
            "Only failed deliveries can be re-dispatched",
            status_code=400,
            error_code="STAFF_DELIVERY_NOT_REDISPATCHABLE",
        ),
    )

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))

    refused = operator.telegram.last_shown()
    assert refused.method == "editMessageText"
    assert refused.text == f"❌ {_copy('staff.error.api.not_redispatchable')}"
    assert refused.callback_data() == [], "a refused card keeps no button to be refused again"
    assert _keyboards(operator) == []
    assert backend_calls(operator, "POST", REDISPATCH) == []
    assert not operator.errors


async def test_a_date_tap_the_backend_refuses_as_past_reads_its_own_cause(operator):
    """Review Focus 1. A date from a step drawn before midnight is in the past by the time
    it lands. The backend refuses it with a code, and the operator reads why in a message
    under the card. A refused date tap never edits the card: see the double-tap journey below."""
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "POST",
        REDISPATCH,
        lambda _call: staff_backend_failure(
            "That date is in the past", status_code=400, error_code="ORDER_RESCHEDULE_DATE_IN_PAST"
        ),
    )

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310314"))

    assert operator.telegram.of("editMessageText", "editMessageReplyMarkup") == []
    assert [call.text for call in operator.telegram.of("sendMessage")] == [
        f"❌ {_copy('staff.error.api.reschedule_date_in_past')}"
    ]
    assert len(operator.telegram.of("answerCallbackQuery")) == 1, (
        "the tap is answered once on entry; a second answer would be a popup Telegram never shows"
    )
    assert alerts(operator) == []
    assert not operator.errors


async def test_a_double_tapped_date_keeps_the_success_reply_under_the_card(operator):
    """Updates in one chat run one at a time (staff_bot/bot.py, PerChatSerialUpdateProcessor).
    So the second tap on the same date posts after the first re-date committed, and the
    backend refuses it: the delivery is no longer failed. That refusal goes under the card
    too, after the success reply. The card still names the order, and the success reply
    stands, because the re-date did go through, and its customer line may be the only
    thing telling the operator to contact the customer."""
    ops, _labels = await sign_in(operator)
    answers = iter(
        [
            _landed("2031-03-22", notifies_customer=True, customer_channel=None),
            staff_backend_failure(
                "Only failed deliveries can be re-dispatched",
                status_code=400,
                error_code="STAFF_DELIVERY_NOT_REDISPATCHABLE",
            ),
        ]
    )
    operator.backend.route("POST", REDISPATCH, lambda _call: next(answers))
    date_tap = f"staff_redispatch_on_{DELIVERY_ID}_20310322"

    await operator.send(ops.tap(date_tap))
    await operator.send(ops.tap(date_tap))

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [
        {"delivery_date": "2031-03-22"},
        {"delivery_date": "2031-03-22"},
    ]
    _assert_the_card_stayed_and_lost_its_buttons(operator)
    assert [call.text for call in operator.telegram.of("sendMessage")] == [
        "\n".join(
            [
                f"✅ {_copy('staff.redispatch.success').format(date='22.03')}",
                f"⚠️ {_copy('staff.redispatch.customer_unreachable')}",
            ]
        ),
        f"❌ {_copy('staff.error.api.not_redispatchable')}",
    ], "the second tap's refusal must not replace the re-date that went through"
    assert not operator.errors


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param((400, {"description": "Bad Request: message to edit not found"}), id="edit-refused"),
        pytest.param(
            (429, {"description": "Too Many Requests: retry after 30", "parameters": {"retry_after": 30}}),
            id="flood-control",
        ),
    ],
)
async def test_a_card_telegram_will_not_clear_still_gets_the_success_reply(operator, refusal):
    """Past the POST the re-date is committed. Taking the card's buttons away is tidy-up,
    so Telegram refusing it must not turn the reply into "an error occurred": that would
    tell the operator the re-date failed, and lose the customer line, the only thing
    telling them to contact a customer the bot cannot reach."""
    status, body = refusal
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "POST", REDISPATCH, lambda _call: _landed("2031-03-22", notifies_customer=True, customer_channel=None)
    )
    operator.telegram.failures["editMessageReplyMarkup"] = lambda _params: (
        status,
        {"ok": False, "error_code": status, **body},
    )

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310322"))

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [{"delivery_date": "2031-03-22"}]
    sent = operator.telegram.of("sendMessage")
    assert [call.text for call in sent] == [
        "\n".join(
            [
                f"✅ {_copy('staff.redispatch.success').format(date='22.03')}",
                f"⚠️ {_copy('staff.redispatch.customer_unreachable')}",
            ]
        )
    ]
    assert sent[0].callback_data() == ["staff_back_to_main"]
    assert _curated("staff.error_occurred", "en") not in [call.text for call in sent]
    assert not operator.errors


async def test_a_transient_backend_failure_leaves_the_card_to_tap_again(operator):
    """A 5xx says nothing about the delivery, so the card and its button stay."""
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "GET", FAILED_ONE, lambda _call: staff_backend_failure("upstream timeout", status_code=503)
    )

    await operator.send(ops.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))

    assert operator.telegram.of("editMessageText", "editMessageReplyMarkup") == []
    assert [call.text for call in operator.telegram.of("sendMessage")] == [
        f"❌ {_curated('staff.error.api.service_unavailable', 'en')}"
    ]


# ---------------------------------------------------------------------------
# The success reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notifies, channel, customer_line",
    [
        pytest.param(True, "telegram", ("📨", "staff.redispatch.customer_notified_telegram"), id="telegram"),
        pytest.param(True, "email", ("📧", "staff.redispatch.customer_notified_email"), id="email"),
        pytest.param(True, None, ("⚠️", "staff.redispatch.customer_unreachable"), id="unreachable"),
        pytest.param(False, None, None, id="not-notified"),
    ],
)
async def test_a_held_re_date_names_the_day_when_drivers_see_it_and_who_was_told(
    operator, notifies, channel, customer_line
):
    """The same three-way customer line as the admin modal's `customerNotice`, read only
    from what the route published for this re-date. It arrives as a new message under
    the card, which keeps naming the customer "contact them yourself" means."""
    ops, _labels = await sign_in(operator)
    operator.backend.route(
        "POST",
        REDISPATCH,
        lambda _call: _landed(
            "2031-03-22", release_at=HELD_RELEASE_AT, notifies_customer=notifies, customer_channel=channel
        ),
    )

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310322"))

    expected = [
        f"✅ {_copy('staff.redispatch.success').format(date='22.03')}",
        _copy("staff.redispatch.success_held").format(date="22.03", time="08:00"),
    ]
    if customer_line is not None:
        emoji, key = customer_line
        expected.append(f"{emoji} {_copy(key)}")
    _assert_the_card_stayed_and_lost_its_buttons(operator)
    sent = operator.telegram.of("sendMessage")
    assert [call.text for call in sent] == ["\n".join(expected)]
    assert sent[0].callback_data() == ["staff_back_to_main"]
    assert not operator.errors


async def test_a_re_date_that_lands_in_todays_pool_says_so(operator):
    ops, _labels = await sign_in(operator)
    operator.backend.route("POST", REDISPATCH, lambda _call: _landed(PUBLISHED_MIN, status="scheduled"))

    await operator.send(ops.tap(f"staff_redispatch_on_{DELIVERY_ID}_20310314"))

    _assert_the_card_stayed_and_lost_its_buttons(operator)
    assert [call.text for call in operator.telegram.of("sendMessage")] == [
        "\n".join(
            [
                f"✅ {_copy('staff.redispatch.success').format(date='14.03')}",
                _copy("staff.redispatch.success_in_pool"),
            ]
        )
    ]


async def test_the_client_posts_a_date_only_when_one_is_given(operator):
    """An old bot posts no body, and the backend reads that as today (spec §9)."""
    async with api_client as client:
        await client.redispatch_delivery("staff-access-token", DELIVERY_ID, date(2031, 3, 22))
        await client.redispatch_delivery("staff-access-token", DELIVERY_ID)

    assert [call.data for call in backend_calls(operator, "POST", REDISPATCH)] == [
        {"delivery_date": "2031-03-22"},
        None,
    ]


# ---------------------------------------------------------------------------
# The list (F9)
# ---------------------------------------------------------------------------


def _failed_list(count: int, total: int) -> dict:
    return {"items": [_row(DELIVERY_ID + n) for n in range(count)], "total": total}


async def test_the_menu_list_names_phone_reason_driver_and_failure_time_and_what_it_left_out(operator):
    ops, _labels = await sign_in(operator)
    page = _failed_list(20, 23)
    page["items"][1]["customer_phone"] = None
    operator.backend.route("GET", FAILED_LIST, lambda _call: page)

    await operator.send(ops.tap("staff_redispatch_failed"))

    # The menu entry keeps editing its header into the menu message.
    assert [call.text for call in operator.telegram.of("editMessageText")] == [
        "\n".join(
            [
                f"🔄 <b>{_copy('staff.redispatch.title')}</b>",
                _copy("staff.redispatch.pick"),
                _copy("staff.redispatch.showing").format(shown=15, total=23),
            ]
        )
    ]
    cards = operator.telegram.of("sendMessage")
    assert len(cards) == 15
    lines = cards[0].text.splitlines()
    # The phone the operator calls to agree the new date, right under the name.
    assert lines[1:3] == ["👤 Dilnoza Rahimova", "📞 +998901112233"]
    assert not [line for line in cards[1].text.splitlines() if line.startswith("📞")], (
        "a customer with no phone gets no empty phone line"
    )
    assert f"❗ {_copy('staff.delivery.reason.customer_unavailable')}" in lines
    assert f"🚚 {_copy('staff.redispatch.driver')}: Bobur Toshmatov" in lines
    assert f"🕒 {_copy('staff.redispatch.failed_at')}: 13.03 14:05" in lines
    assert "customer_unavailable" not in cards[0].text, "the reason code must never reach the operator raw"
    assert [card.callback_data() for card in cards[:2]] == [
        [f"staff_redispatch_do_{DELIVERY_ID}_0"],
        [f"staff_redispatch_do_{DELIVERY_ID + 1}_0"],
    ]
    assert len(operator.telegram.of("answerCallbackQuery")) == 1, "the menu tap must stop its spinner"


async def test_all_failed_deliveries_on_an_alert_posts_new_messages_under_it(operator):
    ops, _labels = await sign_in(operator)
    operator.backend.route("GET", FAILED_LIST, lambda _call: _failed_list(2, 2))

    await operator.send(ops.tap("staff_redispatch_failed_new", message_id=4242))

    assert operator.telegram.of("editMessageText", "editMessageReplyMarkup") == [], (
        "the alert must stay exactly as it was"
    )
    assert len(operator.telegram.of("answerCallbackQuery")) == 1, (
        "the alert's button must stop its spinner, or it spins until Telegram gives up"
    )
    sent = operator.telegram.of("sendMessage")
    # Nothing was left out, so the header says nothing about it.
    assert sent[0].text == "\n".join(
        [f"🔄 <b>{_copy('staff.redispatch.title')}</b>", _copy("staff.redispatch.pick")]
    )
    assert [card.callback_data() for card in sent[1:]] == [
        [f"staff_redispatch_do_{DELIVERY_ID}_0"],
        [f"staff_redispatch_do_{DELIVERY_ID + 1}_0"],
    ]


async def test_every_failure_reason_has_exactly_one_seeded_label():
    """The list card names a reason through one literal key per code, never a key built
    at runtime (Global Constraints, Translations). So the map must cover exactly the
    codes the backend accepts, and every key in it must be seeded in all three languages.

    Imported here, not at the top, so that until Step 6 only this test fails on it.
    """
    from shared.staff_constants import FAILED_DELIVERY_REASONS
    from staff_bot.utils.formatters import _REASON_KEYS

    assert set(_REASON_KEYS) == set(FAILED_DELIVERY_REASONS)
    missing = [
        (key, language)
        for key in _REASON_KEYS.values()
        for language in LANGUAGES
        if not _SEED._curated_value(key, language)
    ]
    assert missing == []


async def test_the_operators_copy_speaks_of_a_new_date_not_a_re_dispatch():
    """Spec §3.5: a failed delivery waits for a new date. Nothing is "re-dispatched" to a
    pool any more, so the menu button, the empty list and the two refusals an operator
    meets on a stale card say what did happen, in every language. The Uzbek for the
    drivers' pool is the bot's own word elsewhere, "havza", never "hovuz" (a swimming pool)."""
    assert _copy("staff.menu.redispatch_failed") == "Failed deliveries"
    assert _copy("staff.redispatch.none") == "No failed deliveries are waiting for a new date."
    keys = (
        "staff.menu.redispatch_failed",
        "staff.redispatch.none",
        "staff.error.api.not_redispatchable",
        "staff.error.api.order_closed_for_redispatch",
    )
    stale = {
        "en": ("dispatch", "Refresh the list"),
        "uz": ("qayta yubor", "Ro'yxatni yangilang"),
        "ru": ("отправ", "Обновите список", "неуданые"),
    }
    leftovers = [
        (key, language, word)
        for key in keys
        for language in LANGUAGES
        for word in stale[language]
        if word.lower() in _curated(key, language).lower()
    ]
    assert leftovers == []
    assert "havza" in _curated("staff.redispatch.success_in_pool", "uz")
    assert "hovuz" not in _curated("staff.redispatch.success_in_pool", "uz")
