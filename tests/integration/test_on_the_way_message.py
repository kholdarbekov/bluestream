"""The driver's "on the way" tap tells the customer when the water arrives (honest-ETA Phase 1, Task 5).

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.8 (S2: the promise made at the tap)
and §4.12a (only the "on the way" step calls Google).

The driver taps "in transit" through the staff bot's own route, `PUT /api/v1/staff/delivery/<id>/status`, with the
body `StaffAPIClient` sends. The commit publishes `send_delivery_update_task(history_id)`. Run as the worker runs it,
the task records the tap's promise (`DeliveryEtaService.record_on_the_way`: Google's 18 minutes, so by = tap + 29)
before it builds the message. The customer's Telegram message then ends with two lines: "Arriving within about 29 min
(by HH:MM)" and "Live traffic: Google Maps". An email customer's template data carries the same two lines as
`eta_line`, and the formula ETA (`estimated_delivery`) is gone from it. Both halves are best-effort: when the promise
cannot be recorded, read back or said, the message still goes out, without a time. A promise ends when the driver sets
off to another stop before arriving (§4.8 S2): from then on Track, and a message built late, show no time.

Real throughout: the route, the history row, the promise rows, the template (the bundled default, or a
`notification_templates` row), the driver-info sanitiser and the persisted `Notification` row. The copy is the shipped
seed's (`seed_translations`), and the expectations render it with the real `get_translation`. Faked, as external I/O
only: Celery publishing (`enqueued` binds each publish against the task's signature), Telegram and Brevo (`live_send`:
the outbound `requests.post`), Google Routes (`google`: an 18-minute drive behind a fake day counter) and the distance
matrix (`matrix`: 11 minutes). A database failure is injected at the SQLAlchemy boundary. The clock the message reads
is pinned one second after the tap (`worker`), so its minutes never depend on how fast a test runs. Every delivery
also carries the formula ETA the dispatcher stamps, two hours out, so a message or an email that still printed it
would show it.
"""

import logging
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from string import Formatter
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from business_app.models.delivery import DeliveryPerson, DeliveryStatusHistory
from business_app.models.delivery_promise import MODEL_GOOGLE_TAP, MODEL_OSRM_SHADOW, DeliveryPromise
from business_app.models.notification import Notification, NotificationTemplate
from business_app.models.translation import Translation
from business_app.services import delivery_eta_service, notification_service
from business_app.services.notification_service import DEFAULT_TEMPLATES, NotificationService
from business_app.tasks.notification_tasks import send_delivery_update_task
from business_app.utils import translations
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.translations import get_translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus
from shared.i18n_rendering import humanise_key
from staff_bot.api_client import StaffAPIClient
from tests.bot_dispatcher_harness import telegram_entity_error
from tests.integration.reorder_reminder_builders import captured_logs, seed_translations
from tests.integration.test_delivery_failed_alert import _worker_runs
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.test_staff_delivery_ownership_guard import _bearer, _delivery, _driver
from tests.integration.test_track_publishes_honest_eta import NOTHING_PROMISED, _eta, _track
from tests.notification_send_fakes import BREVO_URL
from tests.unit.test_delivery_eta_service import (  # noqa: F401 -- google, matrix and spec_tunables are fixtures
    DOOR_AT,
    DRIVER_AT,
    _promises,
    google,
    matrix,
    spec_tunables,
)
from tests.unit.test_delivery_rescheduled_notification import (  # noqa: F401 -- live_send is a fixture
    TELEGRAM_URL,
    _connect_telegram,
    live_send,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("spec_tunables", "shipped_copy")]

STATUS = "/api/v1/staff/delivery/{}/status"
TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)

ON_THE_WAY = "telegram.orders.eta_on_the_way"
ARRIVING_NOW = "telegram.orders.eta_arriving_now"
ATTRIBUTION = "telegram.orders.eta_google_attribution"
ETA_KEYS = (ON_THE_WAY, ARRIVING_NOW, ATTRIBUTION)
IN_TRANSIT_LABEL = "notification.delivery_status.in_transit"
ARRIVED_LABEL = "notification.delivery_status.arrived"

# A raw instant, as the formula ETA used to travel in the template data ("2026-11-03T16:00:00+00:00").
ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
# An arrival key, as a message that printed one would show it.
ETA_KEY = re.compile(r"telegram\.orders\.eta_[a-z0-9_]+")


@pytest.fixture
def shipped_copy(db):
    """The rows the seeder ships for the three ETA keys and the message's status labels."""
    seed_translations(db, (*ETA_KEYS, IN_TRANSIT_LABEL, ARRIVED_LABEL))


@pytest.fixture
def worker(db, enqueued, monkeypatch):  # noqa: F811 -- `enqueued` is the fixture
    """Runs the customer update a tap published, as the worker runs it, reading the clock `read_after` the tap.

    One second after the tap unless a test says otherwise. The message counts its minutes down to the promised time
    from when it is built (`notification_service.get_utc_now`), so a pinned reading keeps them at 29."""

    def run(history, *, read_after=timedelta(seconds=1)):
        call = _published_update(enqueued, history)
        reading = ensure_utc(history.changed_at) + read_after
        monkeypatch.setattr(notification_service, "get_utc_now", lambda: reading)
        return _worker_runs(db, send_delivery_update_task, call)

    return run


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _stop(db, customer, *, number, driver=None):
    """A picked-up stop of `driver`, a fresh driver unless one is given: `_delivery`'s CASH order, with the formula ETA
    the dispatcher stamps.

    `_driver` gives the driver a DeliveryPerson whose id is not their users.id, so a position read by the wrong id
    finds nothing and the message loses its line."""
    if driver is None:
        driver = _driver(db, phone="+998901350001", first_name="Bekzod")
    delivery = _delivery(db, customer, driver, status=DeliveryStatus.PICKED_UP, number=number)
    delivery.estimated_delivery_time = datetime.now(UTC) + timedelta(hours=2)
    db.session.commit()
    return driver, delivery


def _position_shared(db, driver, *, minutes_ago):
    """The driver's live position, at DRIVER_AT, last updated `minutes_ago` minutes ago. The tap follows at once.

    Stamped on the profile `_driver` made. `DeliveryPerson.user_id` is unique, so `_driver_last_seen` of
    tests/unit/test_delivery_eta_service.py, which adds a profile of its own, cannot follow `_driver`."""
    profile = DeliveryPerson.query.filter_by(user_id=driver.id).one()
    profile.current_location_lat, profile.current_location_lng = DRIVER_AT
    profile.last_location_update = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    db.session.commit()


def _driver_taps(app, client, driver, delivery_id, status):
    """The staff bot's status tap, with the body `StaffAPIClient.update_delivery_status` sends. Returns the history
    row it wrote."""
    response = client.put(
        STATUS.format(delivery_id),
        json=StaffAPIClient.delivery_status_body(status),
        headers=_bearer(app, driver),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["status"] == status
    return DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus(status)).one()


def _published_update(published, history):
    """The customer update `history`'s tap published, as `(args, kwargs)`: what the worker runs. `published` is the
    `enqueued` recording, and it holds exactly one update for that history row."""
    updates = [(args, kwargs) for name, args, kwargs in published if name == send_delivery_update_task.name]
    call = ((history.id,), {})
    assert updates.count(call) == 1, updates
    return call


def _telegram_texts(post):
    """The text of every message sent to the customer's chat, in order."""
    return [call.kwargs["json"]["text"] for call in post.call_args_list if call.args[0] == TELEGRAM_URL]


def _by_time(tap, minutes):
    """`minutes` after the tap, as HH:MM on the Tashkent clock."""
    return (ensure_utc(tap.changed_at) + timedelta(minutes=minutes)).astimezone(TASHKENT).strftime("%H:%M")


def _promised_lines(language, tap):
    """The two lines the shipped copy says one second after the tap, for the spec's worked numbers (§4.8 S2): by = the
    tap + ceil(5 off-road + 1.3 seed headroom x 18 Google minutes) = the tap + 29, so 29 minutes left."""
    return [
        get_translation(ON_THE_WAY, language, minutes=29, time=_by_time(tap, 29)),
        get_translation(ATTRIBUTION, language),
    ]


def _shipped_sentence(key, language):
    """The shipped sentence of `key` as a pattern: its words verbatim, anything in each placeholder."""
    words = [
        re.escape(literal)
        for literal, _field, _spec, _conversion in Formatter().parse(BACKEND_TRANSLATIONS[key][language])
    ]
    return re.compile(".+?".join(words))


def _eta_sentences_in(text, language):
    """The ETA keys whose shipped sentence `text` carries, whatever fills their placeholders."""
    return [key for key in ETA_KEYS if _shipped_sentence(key, language).search(text)]


def _went_out_without_a_time(text):
    """The English in-transit update went out, with no arrival line in it and nothing standing in for one: no shipped
    sentence, no arrival key, no humanised key and no raw placeholder."""
    assert get_translation(IN_TRANSIT_LABEL, "en") in text
    assert _eta_sentences_in(text, "en") == []
    assert not ETA_KEY.search(text), text
    assert [key for key in ETA_KEYS if humanise_key(key) in text] == []
    assert NotificationService._unrendered_placeholders(text) == []


def _warnings(caplog, message):
    """The captured WARNING records that read exactly `message`."""
    return [record for record in caplog.records if record.levelno == logging.WARNING and record.getMessage() == message]


@contextmanager
def _copy_logs(caplog):
    """WARNINGs of the send path (`notification_service`) and of the backend copy renderer
    (`business_app.utils.translations.renderer`), which says which copy it could not render."""
    with (
        captured_logs(caplog, notification_service.logger, logging.WARNING),
        captured_logs(caplog, translations.logger, logging.WARNING),
    ):
        yield


@contextmanager
def _the_database_refuses_promises():
    """Every promise row is refused at the INSERT, as a database that is down or locked refuses it."""

    def refuse(mapper, connection, target):
        raise OperationalError("INSERT INTO delivery_promises", {}, Exception("database is locked"))

    event.listen(DeliveryPromise, "before_insert", refuse)
    try:
        yield
    finally:
        event.remove(DeliveryPromise, "before_insert", refuse)


@contextmanager
def _reading_promises_fails_once_they_are_written(engine):
    """Once the promise rows are written, the next read of them fails, and the transaction stays aborted until it is
    rolled back, as Postgres keeps it: every statement before the rollback fails too."""
    state = {"armed": False, "aborted": False}

    def arm(mapper, connection, target):
        state["armed"] = True

    def fail(conn, cursor, statement, parameters, context, executemany):
        if state["aborted"]:
            raise OperationalError(statement, parameters, Exception("current transaction is aborted"))
        if state["armed"] and statement.lstrip().upper().startswith("SELECT") and "delivery_promises" in statement:
            state.update(armed=False, aborted=True)
            raise OperationalError(statement, parameters, Exception("server closed the connection unexpectedly"))

    def rolled_back(conn):
        state["aborted"] = False

    event.listen(DeliveryPromise, "after_insert", arm)
    event.listen(engine, "before_cursor_execute", fail)
    event.listen(engine, "rollback", rolled_back)
    try:
        yield
    finally:
        event.remove(engine, "rollback", rolled_back)
        event.remove(engine, "before_cursor_execute", fail)
        event.remove(DeliveryPromise, "after_insert", arm)


# --------------------------------------------------------------------------- #
# 1. The message says when the water arrives
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_message_tells_the_customer_when_it_arrives(
    app, client, db, sample_user, live_send, google, matrix, worker, language  # noqa: F811
):
    _connect_telegram(db, sample_user, language=language)
    driver, delivery = _stop(db, sample_user, number=f"ORD-ETA-{language.upper()}")
    _position_shared(db, driver, minutes_ago=2)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    # The tap's promise: one shown Google row, and our own engine's hidden time for the same leg beside it.
    [shown] = _promises(MODEL_GOOGLE_TAP)
    [shadow] = _promises(MODEL_OSRM_SHADOW)
    assert (shown.delivery_status_history_id, shown.shown, shown.source) == (tap.id, True, "google_traffic")
    assert ensure_utc(shown.window_end) == ensure_utc(tap.changed_at) + timedelta(minutes=29)
    assert (shadow.delivery_status_history_id, shadow.shown) == (tap.id, False)
    assert len(google.requests) == 1 and len(matrix.asks) == 1
    # The message ends with the promise and its attribution, each a whole line: the driver-info sanitiser, which keeps
    # or drops whole lines, kept both.
    [text] = _telegram_texts(live_send)
    lines = text.splitlines()
    assert lines[-2:] == _promised_lines(language, tap)
    # The copy really rendered: the sentence carries its minutes and its time, and no placeholder is left anywhere.
    assert "29" in lines[-2] and _by_time(tap, 29) in lines[-2]
    assert NotificationService._unrendered_placeholders(text) == []
    # An hour and a minute on the customer's clock, never an instant.
    assert not ISO_TIMESTAMP.search(text), text


def test_a_regional_language_code_gets_the_whole_message_in_its_language(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """`preferred_language` stores Telegram's `language_code` as it comes ("ru-RU"). The whole message is in its base
    language, Russian: the template's header and labels, the status, and the arrival lines. Never the Uzbek the
    template falls back to for a code it does not know, beside Russian lines."""
    _connect_telegram(db, sample_user, language="ru-RU")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-RU-RU")
    _position_shared(db, driver, minutes_ago=2)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    [text] = _telegram_texts(live_send)
    template = DEFAULT_TEMPLATES[("delivery_update", "telegram")]["translations"]["ru"]["content"]
    russian = template.format(
        order_number="ORD-ETA-RU-RU",
        delivery_status=get_translation(IN_TRANSIT_LABEL, "ru"),
        tracking_code=delivery.tracking_number,
    )
    assert text.splitlines() == [*russian.splitlines(), "", *_promised_lines("ru", tap)]


def test_a_stale_position_still_says_when_it_arrives_on_our_own_engines_time(
    app, client, db, sample_user, live_send, google, matrix, worker, monkeypatch  # noqa: F811
):
    """A position more than 10 minutes old at the tap is not where the driver is, so Google is not asked. The customer
    is told a time all the same: our own engine's 11 minutes from that position, plus the 30-minute allowance, so the
    tap + 41. No "Live traffic: Google Maps" under it, as Google gave none of it."""
    # The position is from the tap's own Tashkent day, also when this runs in the minutes after local midnight.
    monkeypatch.setattr(delivery_eta_service, "local_date", lambda _at: date(2026, 11, 4))
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-STALE")
    _position_shared(db, driver, minutes_ago=11)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    [text] = _telegram_texts(live_send)
    assert text.splitlines()[-1] == get_translation(ON_THE_WAY, "en", minutes=41, time=_by_time(tap, 41))
    assert get_translation(ATTRIBUTION, "en") not in text
    assert google.requests == [] and google.calls_counted == 0
    door = delivery.order.delivery_address
    assert matrix.asks == [([DRIVER_AT, (door.latitude, door.longitude)], True)]
    [shown] = _promises(MODEL_GOOGLE_TAP)
    assert (shown.source, shown.expected_at) == ("stale_estimate", None)
    assert ensure_utc(shown.window_end) == ensure_utc(tap.changed_at) + timedelta(minutes=41)


@pytest.mark.parametrize(
    "from_status, to_status, metadata",
    [
        (DeliveryStatus.IN_TRANSIT, "arrived", None),
        (DeliveryStatus.ARRIVED, "delivered", {"cash_collected": 150000.0}),
    ],
    ids=["arrived", "delivered"],
)
def test_the_drivers_tap_through_the_real_route_moves_them_to_the_door(
    app, client, db, sample_user, enqueued, from_status, to_status, metadata  # noqa: F811
):
    """The owner's rule: a driver who taps Arrived or Delivered is at that order's address, so that is where they are
    now, though their last position is only five minutes old. Through the bot's own route, whose JWT identity reaches
    the history row as a string."""
    driver = _driver(db, phone="+998901350002", first_name="Door")
    delivery = _delivery(db, sample_user, driver, status=from_status, number=f"ORD-DOOR-{to_status}")
    _position_shared(db, driver, minutes_ago=5)
    door = delivery.order.delivery_address
    door_at = (door.latitude, door.longitude)
    assert door_at != DRIVER_AT

    response = client.put(
        STATUS.format(delivery.id),
        json=StaffAPIClient.delivery_status_body(to_status, metadata),
        headers=_bearer(app, driver),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    profile = DeliveryPerson.query.filter_by(user_id=driver.id).one()
    assert (profile.current_location_lat, profile.current_location_lng) == door_at


def test_the_stop_just_finished_starts_the_next_leg(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """Spec §4.8 S2: the leg starts where the driver was at the tap. Their last position is 22 minutes old, and their
    arrival at stop A, through the real route, moves it to A's door. The driver then sets off to stop B. B's leg starts
    at A's door, and B's message says when the water arrives."""
    _connect_telegram(db, sample_user, language="en")
    driver, stop_a = _stop(db, sample_user, number="ORD-ETA-STOP-A")
    _, stop_b = _stop(db, sample_user, number="ORD-ETA-STOP-B", driver=driver)
    # A's door moves to DOOR_AT, so a leg from it can be told from one from B's door (`_delivery`'s) or DRIVER_AT.
    stop_a.order.delivery_address.latitude, stop_a.order.delivery_address.longitude = DOOR_AT
    db.session.commit()
    _position_shared(db, driver, minutes_ago=22)

    _driver_taps(app, client, driver, stop_a.id, "in_transit")
    arrival = _driver_taps(app, client, driver, stop_a.id, "arrived")
    tap = _driver_taps(app, client, driver, stop_b.id, "in_transit")
    worker(tap)

    assert (arrival.location_lat, arrival.location_lng) == DOOR_AT
    profile = DeliveryPerson.query.filter_by(user_id=driver.id).one()
    assert (profile.current_location_lat, profile.current_location_lng) == DOOR_AT
    [route] = [request["json"] for request in google.requests]
    assert route["origin"]["location"]["latLng"] == {"latitude": DOOR_AT[0], "longitude": DOOR_AT[1]}
    b_door = stop_b.order.delivery_address
    assert route["destination"]["location"]["latLng"] == {"latitude": b_door.latitude, "longitude": b_door.longitude}
    [text] = _telegram_texts(live_send)  # the worker ran for B's tap alone
    assert text.splitlines()[-2:] == _promised_lines("en", tap)


def test_a_database_template_row_still_gets_the_line(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """Review Focus #4: production renders the update from its own `notification_templates` row, whose copy knows
    nothing of the ETA. Both lines end whatever the row renders."""
    db.session.add(
        NotificationTemplate(
            name="delivery_update_telegram",
            notification_type="delivery_update",
            channel="telegram",
            content="🚚 <b>Your order is on its way</b>\n\nOrder #{order_number}: {delivery_status}",
            is_active=True,
        )
    )
    db.session.commit()
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-ROW")
    _position_shared(db, driver, minutes_ago=2)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    [text] = _telegram_texts(live_send)
    assert text.splitlines() == [
        "🚚 <b>Your order is on its way</b>",
        "",
        f"Order #ORD-ETA-ROW: {get_translation(IN_TRANSIT_LABEL, 'en')}",
        *_promised_lines("en", tap),
    ]


def test_a_database_template_that_places_the_line_itself_shows_it_once(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """A row that prints `{eta_line}` keeps the lines where it put them, and they are not appended a second time."""
    db.session.add(
        NotificationTemplate(
            name="delivery_update_telegram",
            notification_type="delivery_update",
            channel="telegram",
            content="🚚 <b>Your order is on its way</b>\n{eta_line}\n\nOrder #{order_number}: {delivery_status}",
            is_active=True,
        )
    )
    db.session.commit()
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-OWN")
    _position_shared(db, driver, minutes_ago=2)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    [text] = _telegram_texts(live_send)
    assert text.splitlines() == [
        "🚚 <b>Your order is on its way</b>",
        *_promised_lines("en", tap),
        "",
        f"Order #ORD-ETA-OWN: {get_translation(IN_TRANSIT_LABEL, 'en')}",
    ]


def test_rerunning_the_worker_keeps_one_promise(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """Review Focus #3: Celery's acks_late redelivers the same publish. The second run asks Google nothing, writes
    nothing, and says the same promise."""
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-AGAIN")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    worker(tap)
    worker(tap)  # the same publish, redelivered

    assert len(_promises(MODEL_GOOGLE_TAP)) == 1
    assert len(_promises(MODEL_OSRM_SHADOW)) == 1
    assert len(google.requests) == 1 and google.calls_counted == 1
    assert len(matrix.asks) == 1
    lines = _promised_lines("en", tap)
    assert [text.splitlines()[-2:] for text in _telegram_texts(live_send)] == [lines, lines]


def test_the_arrived_message_has_no_time(app, client, db, sample_user, live_send, google, matrix, worker):  # noqa: F811
    """The promise of the trip, still in the future, is not repeated once the driver is at the door. Only the "on the
    way" step calls Google (spec §4.12a)."""
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-DOOR")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    arrived = _driver_taps(app, client, driver, delivery.id, "arrived")
    worker(arrived)

    on_the_way, at_the_door = _telegram_texts(live_send)
    assert on_the_way.splitlines()[-2:] == _promised_lines("en", tap)
    assert get_translation(ARRIVED_LABEL, "en") in at_the_door
    assert _eta_sentences_in(at_the_door, "en") == []
    assert len(google.requests) == 1


def test_an_email_customer_gets_the_line_in_the_email(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """No bot, so the update goes by email. The email Brevo is handed prints both lines, one under the other, and its
    template data, persisted on the `Notification` row, carries them, no longer the formula ETA, which the email
    showed as a raw instant."""
    sample_user.preferred_language = "en"
    db.session.commit()
    assert sample_user.telegram_id is None and sample_user.email
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-MAIL")
    _position_shared(db, driver, minutes_ago=2)

    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    worker(tap)

    assert _telegram_texts(live_send) == []
    [email] = [call for call in live_send.call_args_list if call.args[0] == BREVO_URL]
    assert email.kwargs["json"]["to"][0]["email"] == sample_user.email
    # The sentences carry no character HTML would escape, so they reach the HTML as the copy says them.
    assert "<br>".join(_promised_lines("en", tap)) in email.kwargs["json"]["htmlContent"]
    assert not ISO_TIMESTAMP.search(email.kwargs["json"]["htmlContent"])
    [row] = Notification.query.filter_by(channel="email").all()
    assert row.is_sent is True
    assert row.extra_data["eta_line"] == "\n".join(_promised_lines("en", tap))
    assert "estimated_delivery" not in row.extra_data


# --------------------------------------------------------------------------- #
# 2. What the worker does when it runs late, or the promise cannot be stored or read
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "read_after, first_line",
    [
        pytest.param(
            timedelta(minutes=29, seconds=-30),
            lambda tap: get_translation(ON_THE_WAY, "ru", minutes=1, time=_by_time(tap, 29)),
            id="thirty-seconds-before-the-promised-time",
        ),
        pytest.param(
            timedelta(minutes=30),
            lambda tap: get_translation(ARRIVING_NOW, "ru"),
            id="a-minute-past-the-promised-time",
        ),
    ],
)
def test_a_late_worker_says_what_is_left_of_the_promise(
    app, client, db, sample_user, live_send, google, matrix, worker, read_after, first_line  # noqa: F811
):
    """A queue backlog: the update is built late. Half a minute before the promised "by" (the tap + 29) one minute is
    left, rounded up, and the time still shows; once it has passed, the water is arriving any minute."""
    _connect_telegram(db, sample_user, language="ru")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-LATE")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    worker(tap, read_after=read_after)

    [text] = _telegram_texts(live_send)
    assert text.splitlines()[-2:] == [first_line(tap), get_translation(ATTRIBUTION, "ru")]
    assert NotificationService._unrendered_placeholders(text) == []


def test_a_worker_that_runs_after_the_arrival_asks_google_nothing(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """The in-transit update runs only after the driver already tapped "arrived". The leg is over: no call is spent
    on it, nothing is promised, and the late in-transit message has no time in it."""
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-OVER")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")
    _driver_taps(app, client, driver, delivery.id, "arrived")

    worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    assert google.requests == [] and matrix.asks == []
    assert DeliveryPromise.query.count() == 0


def test_a_promise_the_database_refuses_still_sends_the_message(
    app, client, db, sample_user, live_send, google, matrix, worker, caplog  # noqa: F811
):
    """Recording the promise is best-effort. When its commit fails, a warning says so, the session is rolled back for
    the send path that reads after it, and the message goes out without a time."""
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-REFUSED")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    with captured_logs(caplog, notification_service.logger, logging.WARNING), _the_database_refuses_promises():
        worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    assert DeliveryPromise.query.count() == 0
    [warning] = _warnings(caplog, f"Arrival promise not recorded for history {tap.id}")
    assert warning.exc_info[0] is OperationalError


def test_a_promise_the_database_cannot_read_back_still_sends_the_message(
    app, client, db, sample_user, live_send, google, matrix, worker, caplog  # noqa: F811
):
    """Saying the promise is best-effort too. It is recorded, then the read of it for the message fails. A warning says
    so, the session is rolled back for the send path that reads after it, and the message goes out without a time."""
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-UNREAD")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    with (
        captured_logs(caplog, notification_service.logger, logging.WARNING),
        _reading_promises_fails_once_they_are_written(db.engine),
    ):
        worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    [shown] = _promises(MODEL_GOOGLE_TAP)  # recorded: only the read failed
    assert ensure_utc(shown.window_end) == ensure_utc(tap.changed_at) + timedelta(minutes=29)
    [warning] = _warnings(caplog, f"Arrival line not built for delivery {delivery.id}")
    assert warning.exc_info[0] is OperationalError
    assert _warnings(caplog, f"Arrival promise not recorded for history {tap.id}") == []


# --------------------------------------------------------------------------- #
# 3. Copy the backend cannot say never reaches the customer
# --------------------------------------------------------------------------- #


def test_arrival_copy_with_no_row_never_reaches_the_customer(
    app, client, db, sample_user, live_send, google, matrix, worker, caplog  # noqa: F811
):
    """The backend shipped before the seed ran: no row of the three keys in any language. A time is promised, and the
    message goes out without it: never a dotted key, nor the humanised key that stands in for missing copy."""
    Translation.query.filter(Translation.key.in_(ETA_KEYS)).delete(synchronize_session=False)
    db.session.commit()
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-NOCOPY")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    with _copy_logs(caplog):
        worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    [shown] = _promises(MODEL_GOOGLE_TAP)
    assert shown.window_end is not None
    assert len(_warnings(caplog, f"Arrival copy {ON_THE_WAY} has no row in any language")) == 1


@pytest.mark.parametrize(
    "edited, read_failures",
    [
        pytest.param("Arriving within about {min} min (by {time})", [], id="a-placeholder-nothing-fills"),
        # `get_translation` itself raises on a positional placeholder (IndexError): the lookup fails, and the
        # renderer says so, instead of leaving it to the line's best-effort guard.
        pytest.param("Arriving within about {0} min (by {time})", [IndexError], id="a-positional-placeholder"),
    ],
)
def test_arrival_copy_its_values_cannot_fill_never_reaches_the_customer(
    app, client, db, sample_user, live_send, google, matrix, worker, caplog, edited, read_failures  # noqa: F811
):
    """An admin edited the sentence into a placeholder nothing fills. The message goes out without the line, rather
    than with raw braces or a humanised key, and without the attribution of a time it no longer shows. The copy is
    what failed, not the line, which was built. A lookup that raises is logged by the backend renderer, and that is
    asserted for the positional case; a template its values cannot fill is logged by `shared.i18n_rendering`."""
    sentence = Translation.query.filter_by(key=ON_THE_WAY, language="en").one()
    sentence.value = edited
    db.session.commit()
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-BRACES")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    with _copy_logs(caplog):
        worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    assert "Arriving within about" not in text
    [shown] = _promises(MODEL_GOOGLE_TAP)
    assert shown.window_end is not None
    assert _warnings(caplog, f"Arrival line not built for delivery {delivery.id}") == []
    failed_reads = _warnings(caplog, f"Arrival copy {ON_THE_WAY} could not be read")
    assert [warning.exc_info[0] for warning in failed_reads] == read_failures


def _telegram_parses_html(post):
    """`post`, the `live_send` fake, answers each send as Telegram does: a message whose HTML Telegram cannot parse is
    refused whole, with a 400 (`telegram_entity_error`, a port of tdlib's parser). Every other send is accepted."""
    accepted = post.return_value

    def answer(url, *, json=None, **_kwargs):
        error = telegram_entity_error(json["text"], json.get("parse_mode")) if url == TELEGRAM_URL else None
        if error is None:
            return accepted
        refused = Mock(status_code=400)
        refused.json.return_value = {"ok": False, "error_code": 400, "description": error}
        return refused

    post.side_effect = answer


@pytest.mark.parametrize(
    "edited, sent_as",
    [
        pytest.param("Live traffic & Google Maps", "Live traffic &amp; Google Maps", id="an-ampersand"),
        # An unescaped "<" opens no tag Telegram knows, and Telegram refuses the whole message.
        pytest.param("Live traffic <Google Maps>", "Live traffic &lt;Google Maps&gt;", id="angle-brackets"),
    ],
)
def test_arrival_copy_reaches_telegram_as_text(
    app, client, db, sample_user, live_send, google, matrix, worker, edited, sent_as  # noqa: F811
):
    """The message is sent with parse_mode=HTML, and its arrival lines are admin-editable rows. Each line is escaped on
    its way into the message, so it reads exactly as typed and the message is sent. Only there: `eta_line` itself stays
    as the copy says it, because the email escapes it on its own."""
    attribution = Translation.query.filter_by(key=ATTRIBUTION, language="en").one()
    attribution.value = edited
    db.session.commit()
    _telegram_parses_html(live_send)
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-HTML")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    worker(tap)

    [row] = Notification.query.filter_by(channel="telegram").all()
    assert (row.is_sent, row.failure_reason) == (True, None)
    [text] = _telegram_texts(live_send)
    sentence, _attribution = _promised_lines("en", tap)
    assert text.splitlines()[-2:] == [sentence, sent_as]
    assert row.extra_data["eta_line"] == f"{sentence}\n{edited}"


def test_arrival_copy_that_names_the_driver_sends_neither_line(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """Every line of a delivery message that names the driver is dropped (`_strip_driver_info_from_delivery_message`),
    and the arrival lines are all or nothing (shared/eta_copy.py). An admin edited the sentence to name the driver, so
    the message goes out with neither line: never with the attribution of a time it no longer shows."""
    sentence = Translation.query.filter_by(key=ON_THE_WAY, language="en").one()
    sentence.value = "Your driver arrives within about {minutes} min (by {time})"
    db.session.commit()
    _connect_telegram(db, sample_user, language="en")
    driver, delivery = _stop(db, sample_user, number="ORD-ETA-DRIVER")
    _position_shared(db, driver, minutes_ago=2)
    tap = _driver_taps(app, client, driver, delivery.id, "in_transit")

    worker(tap)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    assert "driver" not in text.lower()
    [row] = Notification.query.filter_by(channel="telegram").all()
    assert row.is_sent is True


# --------------------------------------------------------------------------- #
# 4. A promise ends when the driver sets off to another stop (spec §4.8 S2)
# --------------------------------------------------------------------------- #


def test_setting_off_to_another_stop_hides_the_first_stops_minutes(
    app, client, db, sample_user, auth_headers, live_send, google, matrix, worker  # noqa: F811
):
    """The driver holds two of the customer's stops, sets off to the first, then to the second before reaching the
    first. Drivers reach the newer stop first (prod: 90-97% of the time), so the first stop's promise ends: its Track
    says "on the way" with no minutes, while the second stop's promise stands. Each message said its own promise when
    it went out."""
    _connect_telegram(db, sample_user, language="en")
    driver, first = _stop(db, sample_user, number="ORD-ETA-FIRST")
    _, second = _stop(db, sample_user, number="ORD-ETA-SECOND", driver=driver)
    _position_shared(db, driver, minutes_ago=2)

    first_tap = _driver_taps(app, client, driver, first.id, "in_transit")
    worker(first_tap)
    second_tap = _driver_taps(app, client, driver, second.id, "in_transit")
    worker(second_tap)

    to_first, to_second = _telegram_texts(live_send)
    assert to_first.splitlines()[-2:] == _promised_lines("en", first_tap)
    assert to_second.splitlines()[-2:] == _promised_lines("en", second_tap)
    assert _eta(_track(client, auth_headers, first)) == {**NOTHING_PROMISED, "eta_stage": "on_the_way"}
    second_eta = _eta(_track(client, auth_headers, second))
    assert (second_eta["eta_stage"], second_eta["eta_attribution"]) == ("on_the_way", "google_maps")
    assert second_eta["eta_by_time"] == _by_time(second_tap, 29)
    assert second_eta["eta_minutes_left"] in (28, 29)  # Track reads the real clock: a slow run may let a minute pass


def test_a_late_worker_sends_no_minutes_once_the_driver_set_off_elsewhere(
    app, client, db, sample_user, live_send, google, matrix, worker  # noqa: F811
):
    """A queue backlog: the first stop's update runs only after the driver set off to a second stop and that stop's
    update went out. The first tap's promise is still recorded, but it ended at the second tap, so the first stop's
    message goes out without a time."""
    _connect_telegram(db, sample_user, language="en")
    driver, first = _stop(db, sample_user, number="ORD-ETA-QUEUED")
    _, second = _stop(db, sample_user, number="ORD-ETA-NEWER", driver=driver)
    _position_shared(db, driver, minutes_ago=2)
    first_tap = _driver_taps(app, client, driver, first.id, "in_transit")  # its update waits in the queue

    second_tap = _driver_taps(app, client, driver, second.id, "in_transit")
    worker(second_tap)
    worker(first_tap)

    to_second, to_first = _telegram_texts(live_send)
    assert to_second.splitlines()[-2:] == _promised_lines("en", second_tap)
    _went_out_without_a_time(to_first)
    # Not for want of a promise: the first tap's was recorded with its minutes, and only the rule keeps them back.
    first_promise = DeliveryPromise.query.filter_by(
        delivery_status_history_id=first_tap.id, model_version=MODEL_GOOGLE_TAP
    ).one()
    assert ensure_utc(first_promise.window_end) == ensure_utc(first_tap.changed_at) + timedelta(minutes=29)


# --------------------------------------------------------------------------- #
# 5. Only the delivery's own driver's tap earns minutes (spec §4.8 S2)
# --------------------------------------------------------------------------- #

ADMIN_DELIVERY = "/api/v1/admin/deliveries/{}"


def test_an_admins_in_transit_mark_sends_no_time_and_asks_google_nothing(
    app,
    client,
    db,
    sample_user,
    admin_user,
    admin_claim_headers,
    auth_headers,
    live_send,  # noqa: F811
    google,  # noqa: F811
    matrix,  # noqa: F811
    worker,
):
    """An admin marks the stop in transit from the admin Deliveries page, with the body that page sends. A mark says
    neither where the driver is nor that they set off to this stop. Google is not asked, the message goes out without a
    time, and Track says "on the way" with no minutes. The driver's position is fresh, so only who made the tap
    withholds them."""
    _connect_telegram(db, sample_user, language="en")
    driver, stop = _stop(db, sample_user, number="ORD-ETA-ADMIN")
    _position_shared(db, driver, minutes_ago=2)

    response = client.put(ADMIN_DELIVERY.format(stop.id), json={"status": "in_transit"}, headers=admin_claim_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    mark = DeliveryStatusHistory.query.filter_by(delivery_id=stop.id, new_status=DeliveryStatus.IN_TRANSIT).one()
    assert (mark.old_status, mark.changed_by) == (DeliveryStatus.PICKED_UP, admin_user.id)
    worker(mark)

    [text] = _telegram_texts(live_send)
    _went_out_without_a_time(text)
    assert google.requests == [] and google.calls_counted == 0 and matrix.asks == []
    [shown] = _promises(MODEL_GOOGLE_TAP)
    assert (shown.delivery_status_history_id, shown.source, shown.window_end) == (mark.id, "not_driver_tap", None)
    assert _promises(MODEL_OSRM_SHADOW) == []
    assert _eta(_track(client, auth_headers, stop)) == {**NOTHING_PROMISED, "eta_stage": "on_the_way"}
