"""The customer bot's Track screen says what the backend promised, and counts nothing down (honest-ETA Phase 1, Task 7).

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.8.

`GET /api/v1/orders/<id>/track` publishes the arrival as five finished fields (`eta_stage`, `eta_by`, `eta_by_time`,
`eta_minutes_left`, `eta_attribution`; `DeliveryEtaService.customer_eta`). The bot decides nothing about them:
`shared.eta_copy.track_lines` picks the sentences. The bot adds one thing of its own, the marker of the stage: 🚚 before
the first line while the driver is on the way, 📍 before "Arrived at your address". The hours-and-minutes countdown
("⏰ Estimated remaining: 2h 9m") that counted down to the formula ETA is gone.

Every journey goes through the real PTB dispatcher (tests/telegram_bot/ptb_harness.py) with strict entity parsing, so
a line Telegram's Markdown parser would refuse fails here, and the screens are served the real seeded copy, so every
assertion reads what a customer reads. `test_the_real_track_endpoint_feeds_the_screen` bridges the bot to the real
Flask route, so the fields the route publishes and the way the bot reads them meet.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

# `tests.telegram_bot.ptb_harness` imports the BOT's `i18n`/`config` first, so those names are cached in `sys.modules`
# before anything below can shadow them. `scripts.seed_backend_translations` inserts `/app` at `sys.path[0]` at import
# time (it is also a standalone script), which, if imported first here, repromotes the repo root over `telegram_bot/`
# and breaks every later `from config import config` in this session. See `tests/telegram_bot/conftest.py`.
from tests.telegram_bot.ptb_harness import FakeDatabase, build_bot_harness, menu_already_installed
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from business_app.services import order_service
from business_app.services.delivery_eta_service import SOURCE_STALE_POSITION, DeliveryEtaService
from shared import constants as shared_constants
from shared import eta_copy
from shared.constants import ETA_ATTRIBUTION_GOOGLE, ETA_STAGE_ARRIVED, ETA_STAGE_ON_THE_WAY
from shared.enums import DeliveryStatus
from shared.i18n_rendering import render_translation
from tests.integration.test_track_publishes_honest_eta import _delivery
from tests.unit.test_delivery_eta_service import (  # noqa: F401 -- google, matrix and spec_tunables are fixtures
    TAP,
    _driver_last_seen,
    _moved,
    _promise_of,
    _tap,
    google,
    matrix,
    spec_tunables,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LANGUAGES = ("en", "uz", "ru")

ORDER_ID = 1277
ORDER_NUMBER = "TG_000508_26"
TRACK = "/api/v1/orders/{order_id}/track"

ETA_FIELDS = ("eta_stage", "eta_by", "eta_by_time", "eta_minutes_left", "eta_attribution")
ON_THE_WAY = ETA_STAGE_ON_THE_WAY
ARRIVED = ETA_STAGE_ARRIVED
GOOGLE_MAPS = ETA_ATTRIBUTION_GOOGLE

# The marker the bot puts before the first arrival line, by stage. Written out, not imported: it is what the customer
# sees, so a changed icon fails.
ON_THE_WAY_MARK = "🚚 "
ARRIVED_MARK = "📍 "

KEY_ON_THE_WAY = "telegram.orders.eta_on_the_way"
KEY_ARRIVING_NOW = "telegram.orders.eta_arriving_now"
KEY_ATTRIBUTION = "telegram.orders.eta_google_attribution"
KEY_NO_TIME = "telegram.orders.eta_on_the_way_no_time"
KEY_ARRIVED = "telegram.orders.eta_arrived"
ETA_COPY_KEYS = (KEY_ON_THE_WAY, KEY_ARRIVING_NOW, KEY_ATTRIBUTION, KEY_NO_TIME, KEY_ARRIVED)

# What the screen reads besides the arrival lines: the title, the order number, the timeline and its steps, and the
# buttons under it.
SCREEN_KEYS = (
    "telegram.orders.tracking_title",
    "telegram.order.number",
    "telegram.orders.timeline",
    "telegram.orders.current_status",
    "telegram.orders.status_created",
    "telegram.orders.status_out_for_delivery",
    "telegram.back_to_order",
    "telegram.back",
)

# The screens are served the real seeded copy. The `if` keeps a missing seed from breaking collection: it fails by
# name in `test_the_arrival_copy_is_seeded_where_the_bot_loads_it` instead.
TRANSLATIONS = {
    (language, key): BACKEND_TRANSLATIONS[key][language]
    for key in ETA_COPY_KEYS + SCREEN_KEYS
    for language in LANGUAGES
    if BACKEND_TRANSLATIONS.get(key, {}).get(language)
}


def copy(key: str, language: str, **values) -> str:
    """The seeded copy a customer reads for `key`, rendered by production's own renderer."""
    return render_translation(key, BACKEND_TRANSLATIONS[key][language], (), values)


def track_payload(eta: dict | None, **extra) -> dict:
    """What `GET /api/v1/orders/<id>/track` returns for an order out for delivery.

    The route always publishes the five arrival keys, `None` while nothing is promised; `eta` sets some of them.
    `eta=None` is a body that carries none of the five, as one from before this release does.
    """
    published = {} if eta is None else {**dict.fromkeys(ETA_FIELDS), **eta}
    return {
        "data": {
            "order": {
                "id": ORDER_ID,
                "order_number": ORDER_NUMBER,
                "status": "out_for_delivery",
                "display_status": "out_for_delivery",
                "total_amount": 36000,
                "created_at": "2026-11-03T05:00:00+00:00",
                "payment_info": None,
            },
            "delivery": {"id": 3301, "status": "in_transit"},
            "timeline": [
                {"status": "created", "timestamp": "2026-11-03T05:00:00+00:00", "notes": None, "is_current": False},
                {
                    "status": "out_for_delivery",
                    "timestamp": "2026-11-03T09:00:00+00:00",
                    "notes": None,
                    "is_current": True,
                },
            ],
            **published,
            "payment_timeline": [],
            **extra,
        }
    }


async def customer_bot(monkeypatch, language: str):
    """The real customer bot, for a registered customer reading `language`, who already has the menu keyboard."""
    database = FakeDatabase()
    database.user["preferred_language"] = language
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database, strict_entities=True)
    await menu_already_installed(harness, language)
    return harness


async def track_screen(harness, order_id: int = ORDER_ID):
    """The customer taps Track on `order_id`. Returns the one screen they received, whole: Telegram's Markdown parser
    accepted it."""
    await harness.send(harness.updates().tap(f"track_order_{order_id}"))
    [shown] = harness.telegram.shown
    assert shown.refused is None, shown.refused
    assert shown.params.get("parse_mode") == "Markdown"
    return shown


def arrival_block(shown) -> list[str]:
    """The lines the screen adds under its timeline for the arrival.

    The screen is paragraphs: the title, the order number, the timeline (its header and steps), then the arrival
    lines when the backend published any.
    """
    paragraphs = [paragraph for paragraph in shown.text.split("\n\n") if paragraph.strip()]
    assert len(paragraphs) in (3, 4), paragraphs
    return paragraphs[3].splitlines() if len(paragraphs) == 4 else []


async def served(monkeypatch, language: str, body: dict):
    """A customer bot whose Track call is answered with `body`; they tap Track. Returns the screen."""
    harness = await customer_bot(monkeypatch, language)
    harness.backend.route("GET", TRACK.format(order_id=ORDER_ID), lambda _call: body)
    return await track_screen(harness)


# --------------------------------------------------------------------------- #
# What the screen says
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("language", LANGUAGES)
async def test_on_the_way_shows_minutes_by_time_and_attribution(monkeypatch, language):
    shown = await served(
        monkeypatch,
        language,
        track_payload(
            {
                "eta_stage": ON_THE_WAY,
                "eta_by": "2026-11-03T15:40:00+05:00",
                "eta_by_time": "15:40",
                "eta_minutes_left": 29,
                "eta_attribution": GOOGLE_MAPS,
            }
        ),
    )

    # The marker is before the FIRST line only: the attribution line carries none.
    assert arrival_block(shown) == [
        ON_THE_WAY_MARK + copy(KEY_ON_THE_WAY, language, minutes=29, time="15:40"),
        copy(KEY_ATTRIBUTION, language),
    ]
    assert "⏰" not in shown.text, "the countdown to the formula ETA is gone"
    assert shown.callback_data() == [f"order_{ORDER_ID}", "menu_orders"], "the way back is as it was"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "minutes_left, sentence_key",
    [
        pytest.param(29, KEY_ON_THE_WAY, id="minutes-left"),
        pytest.param(0, KEY_ARRIVING_NOW, id="a-passed-time"),
    ],
)
async def test_a_time_that_is_not_from_google_carries_no_attribution(monkeypatch, language, minutes_left, sentence_key):
    """The attribution line is the backend's `eta_attribution`, read as published and decided by nobody else: a time
    that did not come from Google (another phase's own estimate, say) is not credited to Google Maps."""
    shown = await served(
        monkeypatch,
        language,
        track_payload(
            {
                "eta_stage": ON_THE_WAY,
                "eta_by_time": "15:40",
                "eta_minutes_left": minutes_left,
                "eta_attribution": None,
            }
        ),
    )

    assert arrival_block(shown) == [ON_THE_WAY_MARK + copy(sentence_key, language, minutes=minutes_left, time="15:40")]
    assert copy(KEY_ATTRIBUTION, language) not in shown.text


@pytest.mark.parametrize("language", LANGUAGES)
async def test_a_passed_time_shows_arriving_any_minute(monkeypatch, language):
    """The promised "by" time has passed, so the backend publishes 0 minutes left."""
    shown = await served(
        monkeypatch,
        language,
        track_payload(
            {
                "eta_stage": ON_THE_WAY,
                "eta_by": "2026-11-03T15:40:00+05:00",
                "eta_by_time": "15:40",
                "eta_minutes_left": 0,
                "eta_attribution": GOOGLE_MAPS,
            }
        ),
    )

    assert arrival_block(shown) == [
        ON_THE_WAY_MARK + copy(KEY_ARRIVING_NOW, language),
        copy(KEY_ATTRIBUTION, language),
    ]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "published",
    [
        pytest.param({"eta_stage": ON_THE_WAY}, id="nothing-else-published"),
        # The backend never publishes this, but a time that is not there cannot be credited to Google.
        pytest.param({"eta_stage": ON_THE_WAY, "eta_attribution": GOOGLE_MAPS}, id="an-attribution-without-a-time"),
    ],
)
async def test_on_the_way_without_minutes_shows_on_the_way(monkeypatch, language, published):
    """Review Focus #2: a driver whose last known position was more than 10 minutes old at the tap gets no Google call
    and no minutes. Track says the driver is on the way, with no time and no attribution line."""
    shown = await served(monkeypatch, language, track_payload(published))

    assert arrival_block(shown) == [ON_THE_WAY_MARK + copy(KEY_NO_TIME, language)]
    assert copy(KEY_ATTRIBUTION, language) not in shown.text


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "published",
    [
        pytest.param({"eta_stage": ARRIVED}, id="as-published"),
        # The promise of the trip that has ended must not stand in for the arrival, whatever a body still carries.
        pytest.param(
            {"eta_stage": ARRIVED, "eta_by_time": "15:40", "eta_minutes_left": 9, "eta_attribution": GOOGLE_MAPS},
            id="with-leftover-minutes",
        ),
    ],
)
async def test_arrived_shows_arrived_at_your_address(monkeypatch, language, published):
    shown = await served(monkeypatch, language, track_payload(published))

    assert arrival_block(shown) == [ARRIVED_MARK + copy(KEY_ARRIVED, language)]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(track_payload(dict.fromkeys(ETA_FIELDS)), id="published-empty"),
        pytest.param(track_payload(None), id="not-published"),
        # A stage this bot has no line for says nothing, even with the minutes the body carries.
        pytest.param(
            track_payload(
                {
                    "eta_stage": "somewhere_else",
                    "eta_by_time": "15:40",
                    "eta_minutes_left": 12,
                    "eta_attribution": GOOGLE_MAPS,
                }
            ),
            id="an-unknown-stage",
        ),
        # The countdown a backend from before this release still published: nobody reads it.
        pytest.param(
            track_payload(None, estimated_time_remaining={"total_minutes": 129, "hours": 2}),
            id="the-retired-countdown",
        ),
    ],
)
async def test_no_eta_fields_show_no_time_line(monkeypatch, language, body):
    shown = await served(monkeypatch, language, body)

    assert arrival_block(shown) == []
    # The title carries the screen's only 📍. An arrival line would add a 🚚 (on the way) or a second 📍 (arrived), and
    # the countdown was a ⏰.
    assert shown.text.count("📍") == 1
    assert "🚚" not in shown.text and "⏰" not in shown.text


# --------------------------------------------------------------------------- #
# The marker of each stage
# --------------------------------------------------------------------------- #


def test_every_stage_track_lines_says_a_line_for_has_a_marker():
    """The bot puts the stage's marker before the first arrival line. A stage `shared.eta_copy.track_lines` learns to
    speak for needs a marker in the bot too; this fails the day one is added without it."""
    from handlers.orders import _ETA_STAGE_ICONS

    stages = {value for name, value in vars(shared_constants).items() if name.startswith("ETA_STAGE_")}
    assert {ON_THE_WAY, ARRIVED} <= stages, "the scan no longer finds the stages"

    def render(key, /, **values):
        return copy(key, "en", **values)

    speaking = {
        stage
        for stage in stages
        if eta_copy.track_lines(render, stage=stage, minutes_left=29, by_time="15:40", attribution=GOOGLE_MAPS)
    }

    assert speaking, "track_lines says nothing for any stage, so this pin proves nothing"
    assert speaking <= set(_ETA_STAGE_ICONS), sorted(speaking - set(_ETA_STAGE_ICONS))


async def test_a_stage_with_no_marker_still_shows_its_lines(monkeypatch):
    """A stage `track_lines` speaks for before the bot has a marker for it must not cost the customer the screen:
    its lines show, unmarked."""
    from handlers import orders as orders_module

    monkeypatch.delitem(orders_module._ETA_STAGE_ICONS, ARRIVED)

    shown = await served(monkeypatch, "en", track_payload({"eta_stage": ARRIVED}))

    assert arrival_block(shown) == [copy(KEY_ARRIVED, "en")]


# --------------------------------------------------------------------------- #
# The copy is real copy, where the bot loads it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ETA_COPY_KEYS)
def test_the_arrival_copy_is_seeded_where_the_bot_loads_it(key):
    row = BACKEND_TRANSLATIONS.get(key)
    assert row is not None, f"{key} is rendered by the customer bot but never seeded"
    for language in LANGUAGES:
        assert row.get(language), f"{key} has no {language} copy"
    # telegram_bot/i18n.py loads only category 'telegram'.
    assert _category_for(key) == "telegram"


def test_the_retired_countdown_copy_is_no_longer_seeded():
    assert "telegram.orders.estimated_remaining" not in BACKEND_TRANSLATIONS


# --------------------------------------------------------------------------- #
# End to end: the real route
# --------------------------------------------------------------------------- #

# The clock the route reads, pinned a few minutes after the driver's tap so the minutes never depend on how fast a
# test runs. Google's 18 minutes at the tap promise "by" = tap + 29: 14:29 in Tashkent, 25 minutes from the read.
READ_AT = TAP + timedelta(minutes=4)
PROMISED_BY = TAP + timedelta(minutes=29)


def google_promised(db, delivery, driver) -> None:
    """The driver's tap, and the promise the worker recorded from Google's drive time."""
    _promise_of(db, _tap(db, delivery, driver, at=TAP), window_end=PROMISED_BY)


def position_was_stale(db, delivery, driver) -> None:
    """The driver's tap, made while their last known position was 20 minutes old. Recorded by the real recorder, as the
    notification worker runs it: a shown promise with no time, and no call to Google or to our own engine."""
    _driver_last_seen(db, driver, at=TAP - timedelta(minutes=20))
    tap = _tap(db, delivery, driver, at=TAP)

    shown = DeliveryEtaService().record_on_the_way(tap.id)

    assert (shown.source, shown.expected_at, shown.window_end, shown.shown) == (SOURCE_STALE_POSITION, None, None, True)


def driver_arrived(db, delivery, driver) -> None:
    """The same trip, ended: the driver tapped arrived three minutes after the tap, before the pinned read. The promise
    still has 25 minutes at the read, which a screen that read it would show."""
    google_promised(db, delivery, driver)
    _moved(
        db,
        delivery,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
        at=TAP + timedelta(minutes=3),
        by=driver,
    )


@pytest.mark.parametrize(
    "delivery_status, record, published, lines",
    [
        pytest.param(
            DeliveryStatus.IN_TRANSIT,
            google_promised,
            {"eta_stage": ON_THE_WAY, "eta_minutes_left": 25, "eta_by_time": "14:29", "eta_attribution": GOOGLE_MAPS},
            ["🚚 Arriving within about 25 min (by 14:29)", "Live traffic: Google Maps"],
            id="google-promise",
        ),
        pytest.param(
            DeliveryStatus.IN_TRANSIT,
            position_was_stale,
            {"eta_stage": ON_THE_WAY, "eta_minutes_left": None, "eta_by_time": None, "eta_attribution": None},
            ["🚚 On the way"],
            id="stale-position",
        ),
        pytest.param(
            DeliveryStatus.ARRIVED,
            driver_arrived,
            {"eta_stage": ARRIVED, "eta_minutes_left": None, "eta_by_time": None, "eta_attribution": None},
            ["📍 Arrived at your address"],
            id="arrived",
        ),
    ],
)
async def test_the_real_track_endpoint_feeds_the_screen(
    monkeypatch,
    app,
    db,
    client,
    auth_headers,
    sample_user,
    delivery_driver,
    google,  # noqa: F811 -- the fixture
    matrix,  # noqa: F811 -- the fixture
    spec_tunables,  # noqa: F811 -- the fixture
    delivery_status,
    record,
    published,
    lines,
):
    """An order out for delivery with the formula ETA the dispatcher stamps (two hours out, which the old countdown
    counted down to), the driver's real tap and what the worker recorded for it (the stale-position row is the real
    recorder's own). The bot's Track call is answered by the real route, with the customer's own token; the real route
    publishes the fields and the real bot says them. Google is faked, to be counted: nothing here may ask it."""
    delivery = _delivery(db, sample_user, delivery_driver, delivery_status)
    record(db, delivery, delivery_driver)
    monkeypatch.setattr(order_service, "get_utc_now", lambda: READ_AT)

    harness = await customer_bot(monkeypatch, "en")
    answered: list = []

    def real_route(call):
        assert call.user_token, "Track is reachable only with the customer's token"
        response = client.get(call.endpoint, headers=auth_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        answered.append(response.get_json()["data"])
        return response.get_json()

    harness.backend.route("GET", TRACK.format(order_id=delivery.order_id), real_route)

    shown = await track_screen(harness, delivery.order_id)

    [track] = answered
    assert {key: track[key] for key in published} == published
    assert "estimated_time_remaining" not in track
    assert arrival_block(shown) == lines
    assert "⏰" not in shown.text and "Estimated remaining" not in shown.text
    # Review Focus #2: a stale position gets no Google call, and Track, which only reads, never makes one.
    assert google.requests == [] and google.calls_counted == 0
    assert matrix.asks == []
