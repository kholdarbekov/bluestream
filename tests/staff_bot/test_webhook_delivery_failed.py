"""POST /internal/delivery-failed: the failure alert (spec §4.1, F7), end to end.

The route is reached the way the backend reaches it: a real aiohttp server built by
`StaffWebhookServer.setup()`, a body signed with the webhook secret, the real rate
limiter and the real dedup. The alert leaves through the staff bot's real PTB
`Application` (tests/staff_bot/ptb_harness.py), so what is asserted is what Telegram
received, and the alert's buttons are tapped through the real dispatcher.

The scenarios follow tests/unit/test_staff_sales_webhook.py, with one difference that
is the point of this route: nothing here patches `_is_duplicate_event`. The slot is
claimed BEFORE the send, and releasing it after a failed send is what lets the
backend's retry through; a patched dedup would pass whether or not that retry could
ever send. Redis is an in-memory fake with redis-py's parameter names, or absent,
which is production's degraded path.

Mocked: Telegram (the harness transport), the staff backend API (the harness backend)
and, in the one test that takes its payload from the backend's own tasks, their
outbound `requests.post`.

The copy tests read the seeds directly. The staff seed's Russian gate is called here
because the seed's own `main()` does not run it. The admin hint is compared with the
backend seed's rows for the admin UI labels it quotes.

NOTE: importing the bot module runs setup_logging(), which reconfigures the root
logger and breaks caplog, so log levels are read off an autospec'd module logger.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import create_autospec
from urllib.parse import urlparse

import pytest
from aiohttp import test_utils

from shared.redis_keyspace import RedisKeyspace
from staff_bot import webhook_server as webhook_module
from staff_bot.config import config as staff_config
from staff_bot.webhook_server import StaffWebhookServer

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    _SEED,
    _curated,
    _translation_table,
    build_staff,
    sign_in,
)

# Imported after the bot modules, as tests/telegram_bot/test_webhook_agent_order_proposed.py
# does: importing the backend seed puts `/app` first on `sys.path`.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _resolve_seed_value

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROUTE = "/internal/delivery-failed"
SECRET = "delivery-failed-webhook-test-secret"

# The chat the harness signs its staff member in from: the alert lands there, and
# the journey's taps come from there.
OPERATOR_TELEGRAM_ID = DEFAULT_DRIVER_TELEGRAM_ID
DELIVERY_ID = 9311
# The shape `push_delivery_failed` sends. To this route it is an opaque string.
EVENT_ID = f"delivery-failed:{DELIVERY_ID}:2:{OPERATOR_TELEGRAM_ID}"

LANGUAGES = ("en", "uz", "ru")
# The alert's own rows.
ALERT_COPY_KEYS = (
    "staff.notification.delivery_failed",
    "staff.notification.delivery_failed_reason",
    "staff.notification.delivery_failed_driver",
    "staff.notification.delivery_failed_attempt",
    "staff.notification.delivery_failed_admin_hint",
)
# What the cards below render: those rows, and the two failure reasons the tests post.
ALERT_KEYS = ALERT_COPY_KEYS + (
    "staff.delivery.reason.customer_unavailable",
    "staff.delivery.reason.wrong_address",
)
# Every admin UI label the admin hint quotes, by its backend-seed key. The hint is
# copy, not a lookup, so a renamed label would send admins to a menu that is gone.
ADMIN_HINT_LABEL_KEYS = ("ui.nav.orders", "ui.orders.delivery_failed_only")

OPERATOR_CARD_EN = (
    "⚠️ <b>Delivery failed: order #TG_000508_26 needs a new date</b>\n"
    "👤 Dilnoza &lt;Rahimova&gt; · +998901112244\n"
    "📍 Chilonzor 9-kvartal, 14-uy\n"
    "❗ Reason: Customer unavailable\n"
    "🚚 Driver: Bobur Toshmatov\n"
    "🔁 Attempt: 2"
)
ADMIN_CARD_RU = (
    "⚠️ <b>Доставка не удалась: заказу #TG_000508_26 нужна новая дата</b>\n"
    "👤 Dilnoza &lt;Rahimova&gt; · +998901112244\n"
    "📍 Chilonzor 9-kvartal, 14-uy\n"
    "❗ Причина: Неправильный адрес\n"
    "🚚 Водитель: Bobur Toshmatov\n"
    "🔁 Попытка: 2\n"
    "\n"
    "👉 Назначьте новую дату: Админ-панель → Заказы → «Доставка не удалась»."
)

# One row of `GET /staff/delivery/failed/<id>` (Task 9's `_failed_delivery_row`), for
# the date step the alert's first button opens. Its minimum is no clock's "today":
# the step must draw its dates from here.
FAILED_ROW = {
    "delivery_id": DELIVERY_ID,
    "order_id": 1277,
    "order_number": "TG_000508_26",
    "status": "failed",
    "customer_name": "Dilnoza Rahimova",
    "customer_phone": "+998901112244",
    "address": "Chilonzor 9-kvartal, 14-uy",
    "total_amount": 54000.0,
    "failed_delivery_reason": "customer_unavailable",
    "delivery_attempts": 2,
    "driver_name": "Bobur Toshmatov",
    "failed_at": "2026-10-01T09:40:00+05:00",
    "reschedule_min_date": "2026-10-02",
    "reschedule_max_date": "2026-10-17",
}


def _alert_copy() -> dict:
    """The alert's rows exactly as the seed ships them, in every language."""
    return {(language, key): _curated(key, language) for key in ALERT_KEYS for language in LANGUAGES}


def _payload(**overrides) -> dict:
    """The body `push_delivery_failed` posts (Task 10's keys), a distinct value per field."""
    body = {
        "event_id": EVENT_ID,
        "telegram_id": OPERATOR_TELEGRAM_ID,
        "delivery_id": DELIVERY_ID,
        "order_id": 1277,
        "order_number": "TG_000508_26",
        "reason": "customer_unavailable",
        "driver_name": "Bobur Toshmatov",
        "attempts": 2,
        "customer_name": "Dilnoza <Rahimova>",
        "customer_phone": "+998901112244",
        "address": "Chilonzor 9-kvartal, 14-uy",
        "is_operator": True,
        "language": "en",
    }
    body.update(overrides)
    return body


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _spy_logger(monkeypatch):
    """The module logger, autospec'd: a call `Logger` would refuse raises here too."""
    log = create_autospec(logging.Logger, instance=True)
    monkeypatch.setattr(webhook_module, "logger", log)
    return log


class _FakeRedis:
    """The two calls the dedup makes, under redis-py's own parameter names.

    `set(..., nx=True)` answers True when it claimed the key and None when the key
    was already there: redis-py's contract, and what `_is_duplicate_event` reads.
    """

    def __init__(self):
        self.store = {}

    async def set(self, name, value, ex=None, px=None, nx=False, xx=False):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def delete(self, *names):
        return sum(1 for name in names if self.store.pop(name, None) is not None)


@dataclass
class _Hook:
    harness: Any
    server: StaffWebhookServer
    redis: Optional[_FakeRedis]
    client: Any
    staff: Any = None  # the signed-in operator's update factory, when signed in

    async def post(self, body: dict, *, signature: Optional[str] = None):
        raw = json.dumps(body).encode("utf-8")
        return await self.post_raw(raw, signature or _sign(raw))

    async def post_raw(self, raw: bytes, signature: str, *, path: str = ROUTE):
        response = await self.client.post(
            path,
            data=raw,
            headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature},
        )
        if response.content_type == "application/json":
            return response.status, await response.json()
        return response.status, await response.text()


@asynccontextmanager
async def _webhook(monkeypatch, *, redis_up=True, signed_in=False):
    """The staff bot's webhook server, serving HTTP, wired to a harness Application."""
    harness = await build_staff(
        monkeypatch, roles=["operator"], language="en",
        translations=_translation_table(_alert_copy()),
    )
    staff = (await sign_in(harness))[0] if signed_in else None
    server = StaffWebhookServer()
    redis = _FakeRedis() if redis_up else None

    async def _connect():
        # `_init_redis`'s two outcomes without a network: connected (to the fake),
        # or the in-memory fallback production runs on when Redis is unreachable.
        server._redis, server._redis_connected = redis, redis is not None

    monkeypatch.setattr(server, "_init_redis", _connect)
    server.set_application(harness.application)
    await server.setup()
    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:
        yield _Hook(harness, server, redis, client, staff)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    """The real HMAC check, against a secret this file knows."""
    monkeypatch.setattr(staff_config.security, "webhook_secret", SECRET)


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #


def test_the_alert_copy_passes_the_seeds_own_russian_gate():
    """The seed's rule, called rather than restated. The seed's `main()` has this
    check commented out, so for these rows this test is the only place it runs."""
    _SEED._validate_russian_translations(set(ALERT_COPY_KEYS))


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("label_key", ADMIN_HINT_LABEL_KEYS)
def test_the_admin_hint_quotes_the_admin_uis_own_labels(label_key, language):
    """A label renamed in the backend seed fails here, not in front of an admin."""
    label, _preserve_existing = _resolve_seed_value(BACKEND_TRANSLATIONS[label_key], language)
    assert label in _curated("staff.notification.delivery_failed_admin_hint", language)


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


async def test_an_operator_gets_the_alert_card_with_both_buttons_and_a_sound(monkeypatch):
    async with _webhook(monkeypatch) as hook:
        answer = await hook.post(_payload())

    assert answer == (200, {"success": True, "message": "Notification sent"})
    assert ROUTE in hook.server._rate_limiters
    (sent,) = hook.harness.telegram.of("sendMessage")
    assert sent.params["chat_id"] == OPERATOR_TELEGRAM_ID
    assert sent.params["parse_mode"] == "HTML"
    assert sent.text == OPERATOR_CARD_EN
    # Task 11's alert-card keyboard: the date step for THIS delivery, opened from an
    # alert (origin 1), then the list posted as new messages.
    assert sent.callback_data() == [f"staff_redispatch_do_{DELIVERY_ID}_1", "staff_redispatch_failed_new"]
    # F7: it plays a sound. Nobody is watching this chat when a delivery fails.
    assert sent.params["disable_notification"] is False


async def test_an_admin_who_is_not_an_operator_gets_no_buttons_and_the_admin_path(monkeypatch):
    """`require_operator` refuses every button of the operator card to an admin or
    manager without the operator role. So they get none, plus the one line that says
    where the re-date is done (F7, F8), in their own language."""
    async with _webhook(monkeypatch) as hook:
        answer = await hook.post(_payload(is_operator=False, language="ru", reason="wrong_address"))

    assert answer == (200, {"success": True, "message": "Notification sent"})
    (sent,) = hook.harness.telegram.of("sendMessage")
    assert sent.text == ADMIN_CARD_RU
    assert "reply_markup" not in sent.params
    assert sent.params["disable_notification"] is False


async def test_the_alerts_buttons_reach_the_operator_date_step_for_this_delivery(monkeypatch):
    """A card is only as good as what its buttons land on.

    Every button on the alert must be claimed by a registered handler. "Pick a new
    date" must open the date step for THIS delivery: bounds fetched for it (never read
    off the card or a clock), [Today] on the published minimum, and Back pointing at
    the alert card rather than a list card.
    """
    async with _webhook(monkeypatch, signed_in=True) as hook:
        await hook.post(_payload())
        (alert,) = hook.harness.telegram.of("sendMessage")
        unwired = [
            data for data in alert.callback_data()
            if not hook.harness.handlers_matching(hook.staff.tap(data))
        ]
        hook.harness.backend.route(
            "GET", f"/api/v1/staff/delivery/failed/{DELIVERY_ID}", lambda _call: {"delivery": FAILED_ROW}
        )
        await hook.harness.send(hook.staff.tap(f"staff_redispatch_do_{DELIVERY_ID}_1"))

    assert alert.callback_data(), "the operator card drew no buttons at all"
    assert unwired == [], f"alert buttons no registered handler claims: {unwired}"
    fetches = [
        call for call in hook.harness.backend.calls
        if (call.method, call.endpoint) == ("GET", f"/api/v1/staff/delivery/failed/{DELIVERY_ID}")
    ]
    assert len(fetches) == 1
    step = hook.harness.telegram.of("editMessageReplyMarkup")[-1].callback_data()
    assert f"staff_redispatch_on_{DELIVERY_ID}_20261002" in step
    assert f"staff_redispatch_back_{DELIVERY_ID}_1" in step


# --------------------------------------------------------------------------- #
# The send, and the dedup slot claimed before it
# --------------------------------------------------------------------------- #


async def test_a_transient_send_error_answers_502_and_frees_the_slot_so_the_retry_sends_once(monkeypatch):
    """A failed send that kept its slot would be "Already processed" on every retry:
    the alert lost, and reported delivered."""
    log = _spy_logger(monkeypatch)
    async with _webhook(monkeypatch) as hook:
        hook.harness.telegram.fail("sendMessage", "Bad Gateway", status=502)
        first = await hook.post(_payload())
        after_failure = dict(hook.redis.store)

        hook.harness.telegram.clear_failures()
        hook.harness.telegram.reset()
        retry = await hook.post(_payload())  # push_delivery_failed's retry: the same event_id
        after_retry = dict(hook.redis.store)
        replay = await hook.post(_payload())  # and a replay of a send that landed

    assert first == (502, {"success": False, "message": "Send failed"})
    assert after_failure == {}, "the failed send kept its slot, so every retry is 'Already processed'"
    assert [call.args[0] for call in log.error.call_args_list] == ["%s: %s"]
    log.warning.assert_not_called()
    assert retry == (200, {"success": True, "message": "Notification sent"})
    assert list(after_retry) == [RedisKeyspace.staff_bot_webhook_event(EVENT_ID)]
    assert replay == (200, {"success": True, "message": "Already processed"})
    # Exactly one alert reached the operator across the retry and the replay.
    assert len(hook.harness.telegram.of("sendMessage")) == 1


async def test_a_recipient_who_blocked_the_bot_is_a_200_that_keeps_the_slot(monkeypatch):
    """No retry can reach someone who blocked the bot, so the backend must not retry,
    and the log says WARNING, not ERROR."""
    log = _spy_logger(monkeypatch)
    async with _webhook(monkeypatch) as hook:
        hook.harness.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", status=403)
        first = await hook.post(_payload())
        hook.harness.telegram.reset()
        replay = await hook.post(_payload())

    assert first == (200, {"success": False, "message": "Recipient unreachable"})
    assert [call.args[0] for call in log.warning.call_args_list] == ["%s (recipient unreachable): %s"]
    log.error.assert_not_called()
    assert replay == (200, {"success": True, "message": "Already processed"})
    assert hook.harness.telegram.of("sendMessage") == []


async def test_with_redis_down_a_failed_send_frees_the_in_memory_slot_too(monkeypatch):
    async with _webhook(monkeypatch, redis_up=False) as hook:
        hook.harness.telegram.fail("sendMessage", "Bad Gateway", status=502)
        first = await hook.post(_payload())
        hook.harness.telegram.clear_failures()
        hook.harness.telegram.reset()
        retry = await hook.post(_payload())
        replay = await hook.post(_payload())

    assert first == (502, {"success": False, "message": "Send failed"})
    assert retry == (200, {"success": True, "message": "Notification sent"})
    assert replay == (200, {"success": True, "message": "Already processed"})
    assert len(hook.harness.telegram.of("sendMessage")) == 1


async def test_a_second_failure_of_the_same_delivery_is_a_new_alert_even_with_redis_down(monkeypatch):
    """Review focus 4, bot side. With Redis down, dedup falls back to a key this route
    builds and ignores `event_id`. Without the attempt count in that key, a delivery
    re-dated and failing again would be swallowed as a replay for 24 hours."""
    async with _webhook(monkeypatch, redis_up=False) as hook:
        answers = [
            await hook.post(_payload()),
            await hook.post(_payload(event_id=f"delivery-failed:{DELIVERY_ID}:3:{OPERATOR_TELEGRAM_ID}", attempts=3)),
        ]

    assert answers == [(200, {"success": True, "message": "Notification sent"})] * 2
    sends = hook.harness.telegram.of("sendMessage")
    assert [call.text.rsplit("\n", 1)[-1] for call in sends] == ["🔁 Attempt: 2", "🔁 Attempt: 3"]


async def test_a_payload_without_a_delivery_is_refused_before_a_slot_is_claimed(monkeypatch):
    async with _webhook(monkeypatch) as hook:
        answer = await hook.post(_payload(delivery_id=None))

    assert answer == (400, {"success": False, "message": "Missing or malformed telegram_id or delivery_id"})
    assert hook.redis.store == {}
    assert hook.harness.telegram.of("sendMessage") == []


async def test_a_body_the_signature_does_not_cover_is_refused(monkeypatch):
    forged = json.dumps(_payload()).encode("utf-8")
    signed_for_another_chat = _sign(json.dumps(_payload(telegram_id=1)).encode("utf-8"))
    async with _webhook(monkeypatch) as hook:
        answer = await hook.post_raw(forged, signed_for_another_chat)

    assert answer == (401, {"success": False, "message": "Invalid signature"})
    assert hook.redis.store == {}
    assert hook.harness.telegram.of("sendMessage") == []


# --------------------------------------------------------------------------- #
# Producer to consumer
# --------------------------------------------------------------------------- #


@pytest.fixture
def pushed_alert(monkeypatch, db, sample_order, user_address, delivery_driver, operator_user):
    """What the backend's own tasks post for a real failed delivery, byte for byte.

    `notify_delivery_failed` builds the payload from the rows and publishes one push
    per recipient; the operator is the only one with a Telegram chat. Then
    `push_delivery_failed` re-checks the delivery, signs the body and posts it. Its
    `requests.post` is the one seam, recorded with requests' own signature. The
    fixture is sync on purpose: every backend call runs in the app context that the
    `db` fixture holds.
    """
    from business_app.models.delivery import Delivery
    from business_app.tasks import staff_tasks
    from business_app.tasks.staff_tasks import notify_delivery_failed, push_delivery_failed
    from shared.enums import DeliveryStatus, OrderStatus
    from tests.unit.test_delivery_service_business_rules import _task_spy

    operator_user.telegram_id = str(OPERATOR_TELEGRAM_ID)
    operator_user.preferred_language = "ru"
    sample_order.status = OrderStatus.CONFIRMED
    sample_order.delivery_address_id = user_address.id
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.FAILED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
        failed_delivery_reason="wrong_address",
        delivery_attempts=2,
    )
    db.session.add(delivery)
    db.session.commit()

    pushes = _task_spy(monkeypatch, push_delivery_failed, "delay")
    notify_delivery_failed(delivery.id, delivery_driver.id)
    assert len(pushes) == 1, f"expected one push, to the operator: {pushes}"
    args, kwargs = pushes[0]
    push = inspect.signature(push_delivery_failed.run).bind(*args, **kwargs).arguments

    posted = []

    def _post(url, data=None, json=None, **kwargs):  # requests.post's own parameters
        posted.append((url, json, kwargs["headers"]))
        return SimpleNamespace(status_code=200, text="")

    monkeypatch.setattr(staff_tasks.requests, "post", _post)
    monkeypatch.setattr(staff_tasks, "WEBHOOK_SECRET", SECRET)
    push_delivery_failed(**push)

    [(url, body, headers)] = posted
    return SimpleNamespace(
        path=urlparse(url).path,
        # The bytes `requests` sends for `json=`, which are the bytes that were signed.
        raw=json.dumps(body).encode("utf-8"),
        signature=headers["X-Bot-Webhook-Signature"],
        delivery_id=delivery.id,
        order_number=sample_order.order_number,
        driver_name=delivery_driver.full_name,
    )


async def test_the_backends_own_push_arrives_as_this_card(monkeypatch, pushed_alert):
    """Producer to consumer, with nothing hand-written in between.

    Every other test here posts a payload this file wrote. This one posts exactly what
    the backend's tasks built, signed and addressed. A renamed key, a moved route, or
    a signer and verifier that disagree fail here and nowhere else. The operator reads
    Russian because their `preferred_language` is Russian, and the payload carries it.
    """
    async with _webhook(monkeypatch) as hook:
        answer = await hook.post_raw(pushed_alert.raw, pushed_alert.signature, path=pushed_alert.path)

    assert answer == (200, {"success": True, "message": "Notification sent"})
    (sent,) = hook.harness.telegram.of("sendMessage")
    assert sent.params["chat_id"] == OPERATOR_TELEGRAM_ID
    assert sent.text.startswith(
        f"⚠️ <b>Доставка не удалась: заказу #{pushed_alert.order_number} нужна новая дата</b>"
    )
    assert f"❗ Причина: {_curated('staff.delivery.reason.wrong_address', 'ru')}" in sent.text
    assert f"🚚 Водитель: {pushed_alert.driver_name}" in sent.text
    assert sent.text.endswith("🔁 Попытка: 2")
    assert sent.callback_data() == [
        f"staff_redispatch_do_{pushed_alert.delivery_id}_1", "staff_redispatch_failed_new",
    ]
    assert sent.params["disable_notification"] is False
