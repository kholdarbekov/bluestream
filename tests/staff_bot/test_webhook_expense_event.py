"""POST /internal/expense-event: the office's decision on a claim reaches the driver (spec 2026-11-17).

Reached the way the backend reaches it: a real aiohttp server from `StaffWebhookServer.setup()`, a body signed
with the webhook secret, the real rate limiter and the real dedup (Redis is an in-memory fake). The message
leaves through the staff bot's real PTB `Application`, so what is asserted is what Telegram received. The copy
is the seed's own, through `_curated_value`; the expected texts are literals.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import test_utils

from shared.staff_constants import DRIVER_EXPENSE_EVENTS
from staff_bot import webhook_server as webhook_module
from staff_bot.config import config as staff_config
from staff_bot.webhook_server import StaffWebhookServer

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_operator_journey_dispatcher import _curated, _translation_table, build_staff
from tests.staff_bot.test_webhook_delivery_failed import _FakeRedis

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROUTE = "/internal/expense-event"
SECRET = "expense-event-webhook-test-secret"
LANGUAGES = ("en", "uz", "ru")
CHAT = DEFAULT_DRIVER_TELEGRAM_ID

COPY_KEYS = (
    ("staff.currency.uzs", "staff.expense.category.fuel", "staff.expense.category.other")
    + tuple(f"staff.expense.notify.{event}" for event in DRIVER_EXPENSE_EVENTS)
)

PAYLOADS = {
    "approved": {"expense_id": 41, "category": "fuel", "amount": 150000.0, "approved_amount": 150000.0,
                 "reason": None, "hand_in_now": 250000.0},
    "approved_less": {"expense_id": 41, "category": "fuel", "amount": 150000.0, "approved_amount": 100000.0,
                      "difference": 50000.0, "reason": None, "hand_in_now": 250000.0},
    "rejected": {"expense_id": 41, "category": "other", "amount": 150000.0, "approved_amount": None,
                 "reason": "Not a <real> receipt", "hand_in_now": 400000.0},
    "approval_undone": {"expense_id": 41, "category": "fuel", "amount": 150000.0, "approved_amount": None,
                        "reason": "Wrong amount", "hand_in_now": 100000.0},
}

EXPECTED = {
    ("approved", "en"): "✅ Expense approved · Fuel · 150,000 UZS · Hand in now: 250,000 UZS",
    ("approved", "uz"): "✅ Xarajat tasdiqlandi · Yoqilg'i · 150,000 som · Hozir topshiring: 250,000 som",
    ("approved", "ru"): "✅ Расход утверждён · Топливо · 150,000 сум · Сдать сейчас: 250,000 сум",
    ("approved_less", "en"): "✅ Approved 100,000 UZS of 150,000 UZS · hand in the other 50,000 UZS with your cash",
    ("approved_less", "uz"): "✅ 150,000 som dan 100,000 som tasdiqlandi · qolgan 50,000 som ni naqdingiz bilan topshiring",
    ("approved_less", "ru"): "✅ Утверждено 100,000 сум из 150,000 сум · остальные 50,000 сум сдайте вместе с наличными",
    ("rejected", "en"): "❌ Not approved · Not a &lt;real&gt; receipt · hand in this 150,000 UZS with your cash",
    ("rejected", "uz"): "❌ Tasdiqlanmadi · Not a &lt;real&gt; receipt · bu 150,000 som ni naqdingiz bilan topshiring",
    ("rejected", "ru"): "❌ Не утверждено · Not a &lt;real&gt; receipt · сдайте эти 150,000 сум вместе с наличными",
    ("approval_undone", "en"): "↩️ Approval undone · Fuel 150,000 UZS is waiting for a new decision",
    ("approval_undone", "uz"): "↩️ Tasdiq bekor qilindi · Yoqilg'i 150,000 som yangi qarorni kutmoqda",
    ("approval_undone", "ru"): "↩️ Утверждение отменено · Топливо 150,000 сум ожидает нового решения",
}


def _body(name="approved", **overrides) -> dict:
    body = {
        "event_id": f"expense-event:{name}:41",
        "telegram_id": CHAT,
        "event": name,
        "payload": dict(PAYLOADS[name]),
    }
    body.update(overrides)
    return body


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    monkeypatch.setattr(staff_config.security, "webhook_secret", SECRET)


@asynccontextmanager
async def _webhook(monkeypatch, *, language="en"):
    table = _translation_table({
        (lang, key): _curated(key, lang) for key in COPY_KEYS for lang in LANGUAGES
    })
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language=language, translations=table)
    server = StaffWebhookServer()
    redis = _FakeRedis()

    async def _connect():
        server._redis, server._redis_connected = redis, True

    monkeypatch.setattr(server, "_init_redis", _connect)
    server.set_application(harness.application)
    await server.setup()
    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:
        yield SimpleNamespace(harness=harness, client=client, redis=redis)


async def _post(hook, body, *, signature=None):
    raw = json.dumps(body).encode("utf-8")
    response = await hook.client.post(
        ROUTE, data=raw,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature or _sign(raw)},
    )
    return response.status


def _sent_texts(hook):
    return [call.text for call in hook.harness.telegram.of("sendMessage")]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("event", DRIVER_EXPENSE_EVENTS)
async def test_each_decision_renders_its_message(monkeypatch, event, language):
    async with _webhook(monkeypatch, language=language) as hook:
        assert await _post(hook, _body(event)) == 200

    assert _sent_texts(hook) == [EXPECTED[(event, language)]]


@pytest.mark.parametrize("missing", [("difference",), ("approved_amount", "difference")])
async def test_an_approved_less_push_without_its_figures_renders_no_half_filled_line(monkeypatch, missing):
    body = _body("approved_less")
    for key in missing:
        del body["payload"][key]
    async with _webhook(monkeypatch) as hook:
        assert await _post(hook, body) == 200

    expected = {
        ("difference",): "✅ Expense approved · Fuel · 100,000 UZS · Hand in now: 250,000 UZS",
        ("approved_amount", "difference"): "✅ Expense approved · Fuel · 150,000 UZS · Hand in now: 250,000 UZS",
    }[missing]
    assert _sent_texts(hook) == [expected]


async def test_an_unknown_event_is_refused(monkeypatch):
    async with _webhook(monkeypatch) as hook:
        assert await _post(hook, _body("approved", event="paid_out")) == 400
        assert await _post(hook, _body("approved", telegram_id=None)) == 400

    assert _sent_texts(hook) == []
    assert hook.redis.store == {}


async def test_a_repeated_event_id_is_sent_once(monkeypatch):
    async with _webhook(monkeypatch) as hook:
        assert await _post(hook, _body("approved")) == 200
        assert await _post(hook, _body("approved")) == 200

    assert _sent_texts(hook) == [EXPECTED[("approved", "en")]]


async def test_a_bad_signature_is_refused(monkeypatch):
    async with _webhook(monkeypatch) as hook:
        assert await _post(hook, _body("approved"), signature="0" * 64) == 401

    assert _sent_texts(hook) == []


async def test_the_door_is_rate_limited(monkeypatch):
    # Frozen clock: no token refills between posts, so the burst of 60 is exact.
    monkeypatch.setattr(webhook_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    async with _webhook(monkeypatch) as hook:
        statuses = [
            await _post(hook, _body("approved", event_id=f"expense-event:approved:{n}")) for n in range(61)
        ]

    assert statuses == [200] * 60 + [429]
