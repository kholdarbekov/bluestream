"""Bot webhook route: POST /internal/delivery-completed.

Mirrors payment_success_handler's structure (HMAC verify + dedup + i18n build +
send via the bot Application). Design SSOT:
docs/superpowers/specs/2026-07-11-bottle-delivery-summary-design.md §3.3.

The route lives in the bot process so callback routing for the "Report an issue"
button (callback_data report_issue_{order_id}) is guaranteed.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# tests/staff_bot/conftest.py:21-28 owns the bare `webhook_server` name in
# sys.modules (aliased session-wide to staff_bot.webhook_server), so a bare
# `import webhook_server` here resolves to the STAFF module in any xdist worker
# that loaded a staff_bot test first. A package-qualified
# `import telegram_bot.webhook_server` breaks too: executing its body re-resolves
# its workdir-relative `from config import config` via sys.path, where the staff
# conftest's repo-root insertion shadows telegram_bot/config.py. So instead:
# (1) `import bot` first — every other tests/telegram_bot module does this and
# passes in all full-suite workers; it claims/validates telegram's bare deps
# (config, i18n, database, ...) in sys.modules exactly as in production, where
# webhook_server runs inside the bot process; (2) load webhook_server.py by file
# path under a UNIQUE module name, never touching the staff-owned bare name.
import bot  # noqa: F401

import importlib.util
from pathlib import Path

_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)
WebhookServer = ws_module.WebhookServer


SECRET = "test-webhook-secret"


class FakeRedis:
    """Minimal async Redis fake implementing SET NX EX semantics.

    Returns True the first time a key is claimed and None if it already exists,
    matching redis-py's `set(..., nx=True)` contract that _is_duplicate_webhook
    relies on.
    """

    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


def _make_request(payload, *, request_id="req-default", remote="10.0.0.5", signature=None):
    """Build a fake aiohttp request the handler + verify_webhook_signature can read.

    The signed body is what verify reads via `await request.read()`; the handler
    reads the already-parsed dict via `await request.json()`. Keeping json()
    independent of the signed bytes keeps the two paths consistent regardless of
    json.dumps key ordering.
    """
    body = json.dumps(payload).encode("utf-8")
    if signature is None:
        signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Bot-Webhook-Signature": signature}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return SimpleNamespace(
        headers=headers,
        remote=remote,
        path="/internal/delivery-completed",
        read=AsyncMock(return_value=body),
        json=AsyncMock(return_value=payload),
    )


@pytest.fixture
def ws():
    """A WebhookServer with a fake bot Application (AsyncMock send_message) and a
    fresh FakeRedis behind token_manager for the SET-NX dedup path."""
    server = WebhookServer()
    server.bot_app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={"token_manager": SimpleNamespace(redis=FakeRedis())},
    )
    return server


@pytest.fixture(autouse=True)
def _webhook_env(monkeypatch):
    # Real HMAC verification against a known secret: signed requests pass, a
    # tampered signature fails — exercising the production verify path.
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    # Deterministic i18n: echo "key|lang|param=value|..." so message composition
    # is assertable without DB-backed category='telegram' rows (which only exist
    # after seeding). Set on the instance, so no `self` is passed.
    monkeypatch.setattr(
        ws_module.i18n, "get_user_language", AsyncMock(return_value="en")
    )

    def fake_get(key, language, **kwargs):
        return "|".join([key, language] + [f"{k}={v}" for k, v in kwargs.items()])

    monkeypatch.setattr(ws_module.i18n, "get", fake_get)


@pytest.mark.unit
@pytest.mark.anyio
async def test_valid_request_sends_full_summary(ws):
    payload = {
        "order_id": 1234,
        "order_number": "1234",
        "telegram_id": 55501,
        "bottles_delivered": "4",
        "bottles_collected": "3",
        "balance": "5",
    }

    resp = await ws.delivery_completed_handler(_make_request(payload))

    assert resp.status == 200
    ws.bot_app.bot.send_message.assert_awaited_once()
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 55501
    assert kwargs["parse_mode"] == "HTML"

    text = kwargs["text"]
    lines = text.split("\n")
    assert lines[0] == "telegram.delivery_summary.title|en|order_number=1234"
    assert lines[1] == ""  # blank separator before the bottle block
    assert "telegram.delivery_summary.bottles_delivered|en|count=4" in text
    assert "telegram.delivery_summary.bottles_collected|en|count=3" in text
    assert "telegram.delivery_summary.balance|en|count=5" in text

    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "report_issue_1234"
    assert button.text == "telegram.delivery_summary.report_button|en"


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_message_localized_per_language(ws, monkeypatch, language):
    monkeypatch.setattr(
        ws_module.i18n, "get_user_language", AsyncMock(return_value=language)
    )
    payload = {
        "order_id": 7,
        "order_number": "7",
        "telegram_id": 900,
        "bottles_delivered": "2",
        "bottles_collected": "0",
        "balance": "2",
    }

    resp = await ws.delivery_completed_handler(_make_request(payload))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    text = kwargs["text"]
    assert f"telegram.delivery_summary.title|{language}|order_number=7" in text
    assert f"telegram.delivery_summary.bottles_delivered|{language}|count=2" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == f"telegram.delivery_summary.report_button|{language}"


@pytest.mark.unit
@pytest.mark.anyio
async def test_invalid_signature_returns_401(ws):
    payload = {
        "order_id": 1,
        "order_number": "1",
        "telegram_id": 5,
        "bottles_delivered": "1",
        "bottles_collected": "0",
        "balance": "1",
    }

    resp = await ws.delivery_completed_handler(_make_request(payload, signature="deadbeef"))

    assert resp.status == 401
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_missing_telegram_id_returns_400(ws):
    payload = {
        "order_id": 3,
        "order_number": "3",
        "bottles_delivered": "1",
        "bottles_collected": "0",
        "balance": "1",
    }

    resp = await ws.delivery_completed_handler(_make_request(payload))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_zero_zero_omits_bottle_block_keeps_button(ws):
    payload = {
        "order_id": 9,
        "order_number": "9",
        "telegram_id": 5,
        "bottles_delivered": "0",
        "bottles_collected": "0",
        "balance": "0",
    }

    resp = await ws.delivery_completed_handler(_make_request(payload))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    text = kwargs["text"]
    # Title only — no bottle block, no balance line.
    assert text == "telegram.delivery_summary.title|en|order_number=9"
    assert "bottles_delivered" not in text
    assert "balance" not in text
    # Button still present.
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "report_issue_9"


@pytest.mark.unit
@pytest.mark.anyio
async def test_setup_registers_route(ws):
    await ws.setup()
    routes = {(r.method, r.resource.canonical) for r in ws.app.router.routes()}
    assert ("POST", "/internal/delivery-completed") in routes


@pytest.mark.unit
@pytest.mark.anyio
async def test_duplicate_order_id_is_deduplicated(ws):
    payload = {
        "order_id": 42,
        "order_number": "42",
        "telegram_id": 5,
        "bottles_delivered": "1",
        "bottles_collected": "1",
        "balance": "4",
    }

    r1 = await ws.delivery_completed_handler(_make_request(payload, request_id="retry-A"))
    assert r1.status == 200

    # A backend Celery retry mints a FRESH X-Request-ID (trigger_bot_webhook has
    # no g.request_id outside a Flask request), so request-id dedup can't catch
    # it — the order-id SET-NX layer must, using the SAME FakeRedis on this ws.
    r2 = await ws.delivery_completed_handler(_make_request(payload, request_id="retry-B"))
    assert r2.status == 200

    ws.bot_app.bot.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
async def test_send_failure_releases_order_dedup_so_retry_succeeds(ws):
    payload = {
        "order_id": 4242,
        "order_number": "4242",
        "telegram_id": 5,
        "bottles_delivered": "1",
        "bottles_collected": "1",
        "balance": "4",
    }
    # First attempt: the Telegram send raises → handler 500s. The backend then
    # releases ITS idempotency key and re-POSTs on Celery retry with a FRESH
    # X-Request-ID. That retry can only be caught by the order-id dedup layer,
    # so the order-id key claimed on the first (failed) attempt MUST be released
    # on send failure — otherwise the retry is absorbed and the summary is lost.
    ws.bot_app.bot.send_message = AsyncMock(
        side_effect=[RuntimeError("telegram down"), None]
    )

    r1 = await ws.delivery_completed_handler(_make_request(payload, request_id="retry-A"))
    assert r1.status == 500

    r2 = await ws.delivery_completed_handler(_make_request(payload, request_id="retry-B"))
    assert r2.status == 200

    # First send raised, second send delivered — the retry got through instead
    # of being deduplicated away.
    assert ws.bot_app.bot.send_message.await_count == 2
