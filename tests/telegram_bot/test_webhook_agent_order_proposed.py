"""Bot webhook route: POST /internal/agent-order-proposed.

Mirrors tests/telegram_bot/test_webhook_delivery_completed.py, which is the
precedent for every customer-bot webhook route: real HMAC verification, the
real two-layer dedup, a deterministic i18n echo, and the real
`KeyboardBuilder` serialising the buttons.

The route lives in the bot process for the same reason the delivered summary
does: callback routing for `agent_order_confirm_{order_id}` /
`agent_order_decline_{order_id}` is only guaranteed where the bot's
Application is.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# See test_webhook_delivery_completed.py:20-32 for why this two-step import is
# the only safe way to reach telegram_bot/webhook_server.py from pytest:
# (1) `import bot` claims telegram's bare deps (config, i18n, database, ...) in
# sys.modules exactly as production does; (2) webhook_server.py is loaded BY
# FILE PATH under a module name no other suite owns, so the staff bot's
# session-wide alias of the bare name `webhook_server` cannot shadow it.
import bot  # noqa: F401

import importlib.util
from pathlib import Path

_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location(
    "telegram_webhook_server_agent_order_under_test", _WS_PATH
)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)
WebhookServer = ws_module.WebhookServer


SECRET = "test-webhook-secret"

# One realistic push, exactly as business_app/tasks/sales_agent_tasks.py
# ::push_agent_order_confirmation builds it: `total` a raw float,
# `delivery_date` an ISO date string. No pre-formatted strings cross the wire.
PAYLOAD = {
    "telegram_id": 55501,
    "order_id": 9001,
    "order_number": "SA_000123_26",
    "request_id": "a1b2c3d4e5f60718",
    "agent_name": "Dilshod",
    "items": [
        {"name": "Pure Water 19L", "qty": 4},
        {"name": "Bottle Cap", "qty": 2},
    ],
    "total": 60000.0,
    "delivery_date": "2026-09-10",
}


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
    reads the already-parsed dict via `await request.json()`.
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
        path="/internal/agent-order-proposed",
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
    monkeypatch.setattr(
        ws_module.i18n, "get_user_language", AsyncMock(return_value="en")
    )

    # Deterministic i18n: echo "key|lang|param=value|..." so message composition
    # is assertable without DB-backed category='telegram' rows.
    def fake_get(key, language, **kwargs):
        return "|".join([key, language] + [f"{k}={v}" for k, v in kwargs.items()])

    monkeypatch.setattr(ws_module.i18n, "get", fake_get)


@pytest.mark.unit
@pytest.mark.anyio
async def test_valid_request_sends_the_proposal_with_both_buttons(ws):
    resp = await ws.agent_order_proposed_handler(_make_request(PAYLOAD))

    assert resp.status == 200
    ws.bot_app.bot.send_message.assert_awaited_once()
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 55501
    assert kwargs["parse_mode"] == "HTML"

    text = kwargs["text"]
    assert text.startswith("telegram.agent_order.proposed|en|")
    assert "agent_name=Dilshod" in text
    assert "order_number=SA_000123_26" in text
    # Every line is rendered through the per-line key, then joined — so a
    # second product is a second rendered row, not a hand-built string.
    assert (
        "items=telegram.agent_order.item|en|name=Pure Water 19L|qty=4\n"
        "telegram.agent_order.item|en|name=Bottle Cap|qty=2"
    ) in text
    # The bot formats the raw float and the ISO date with its own helpers.
    assert "total=60,000" in text
    assert "delivery_date=10.09.2026" in text

    confirm, decline = kwargs["reply_markup"].inline_keyboard[0]
    assert confirm.callback_data == "agent_order_confirm_9001"
    assert confirm.text == "telegram.agent_order.confirm_button|en"
    assert decline.callback_data == "agent_order_decline_9001"
    assert decline.text == "telegram.agent_order.decline_button|en"


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_proposal_is_localized_per_language(ws, monkeypatch, language):
    monkeypatch.setattr(
        ws_module.i18n, "get_user_language", AsyncMock(return_value=language)
    )

    resp = await ws.agent_order_proposed_handler(_make_request(PAYLOAD))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith(f"telegram.agent_order.proposed|{language}|")
    assert f"telegram.agent_order.item|{language}|name=Pure Water 19L|qty=4" in kwargs["text"]
    confirm, decline = kwargs["reply_markup"].inline_keyboard[0]
    assert confirm.text == f"telegram.agent_order.confirm_button|{language}"
    assert decline.text == f"telegram.agent_order.decline_button|{language}"


@pytest.mark.unit
@pytest.mark.anyio
async def test_backend_supplied_names_are_html_escaped(ws):
    """`parse_mode='HTML'` is on, and the agent name and product names are free
    text an operator typed. Unescaped, a stray `<` makes Telegram reject the
    whole send with "Can't parse entities" — the customer sees nothing at all.
    """
    payload = dict(PAYLOAD, agent_name="A & B <Sales>", items=[{"name": "19L <XL>", "qty": 1}])

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 200
    text = ws.bot_app.bot.send_message.await_args.kwargs["text"]
    assert "agent_name=A &amp; B &lt;Sales&gt;" in text
    assert "name=19L &lt;XL&gt;" in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_quantity_is_escaped_like_every_other_backend_value(ws):
    """`qty` lands in the same `parse_mode='HTML'` message as `name`, and
    nothing on this route proves it is a number — it is whatever JSON the
    backend serialised. One unescaped '<' anywhere in the text makes Telegram
    reject the WHOLE send with "Can't parse entities": the store sees no
    proposal at all and the order rides the confirmation TTL into an
    auto-confirm nobody agreed to.
    """
    payload = dict(PAYLOAD, items=[{"name": "19L", "qty": "2 <b>free</b>"}])

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 200
    text = ws.bot_app.bot.send_message.await_args.kwargs["text"]
    assert "qty=2 &lt;b&gt;free&lt;/b&gt;" in text
    assert "<b>free</b>" not in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_itemless_payload_renders_a_value_not_a_blank_line(ws):
    """`{items}` sits on its own line between the agent's name and the total
    (scripts/seed_backend_translations.py::'telegram.agent_order.proposed'), so
    an empty join leaves the store staring at a gap where the order should be —
    on the one message that asks them to commit money. A dash is a value, the
    same treatment `delivery_date` already gets two lines below.
    """
    payload = dict(PAYLOAD, items=[])

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 200
    assert "items=—" in ws.bot_app.bot.send_message.await_args.kwargs["text"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_missing_delivery_date_still_renders_a_value(ws):
    """`orders.delivery_date` is nullable, and `render_translation` degrades a
    template whose placeholders it cannot fill to the humanised key — i.e. raw
    English debug text for a real customer. A dash is a value; None is not.
    """
    payload = dict(PAYLOAD, delivery_date=None)

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 200
    assert "delivery_date=—" in ws.bot_app.bot.send_message.await_args.kwargs["text"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_invalid_signature_returns_401(ws):
    resp = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, signature="deadbeef")
    )

    assert resp.status == 401
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_missing_telegram_id_returns_400(ws):
    payload = {k: v for k, v in PAYLOAD.items() if k != "telegram_id"}

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_missing_order_id_returns_400(ws):
    payload = {k: v for k, v in PAYLOAD.items() if k != "order_id"}

    resp = await ws.agent_order_proposed_handler(_make_request(payload))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_setup_registers_route(ws):
    await ws.setup()
    routes = {(r.method, r.resource.canonical) for r in ws.app.router.routes()}
    assert ("POST", "/internal/agent-order-proposed") in routes


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_second_push_for_the_same_order_is_deduplicated(ws):
    """Layer 2 (order_id). A re-push carrying a DIFFERENT request id — a second
    confirmation row minted for the same order, or an operator-triggered
    repush — must not put a second proposal in front of the store.

    (Celery's own retries carry the SAME id and are collapsed by layer 1
    instead; see test_send_failure_releases_both_dedup_layers_… for why that
    distinction is load-bearing.)
    """
    r1 = await ws.agent_order_proposed_handler(_make_request(PAYLOAD, request_id="push-A"))
    assert r1.status == 200

    r2 = await ws.agent_order_proposed_handler(_make_request(PAYLOAD, request_id="push-B"))
    assert r2.status == 200
    # The BODY is load-bearing, not just the status: the backend task retries
    # three times on a `success: False` result
    # (business_app/tasks/sales_agent_tasks.py::push_agent_order_confirmation),
    # so a suppressed duplicate that answers falsy re-queues the very push it
    # just suppressed.
    assert json.loads(r2.body)["success"] is True

    ws.bot_app.bot.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_exact_http_replay_is_deduplicated(ws):
    """Layer 1: one Celery attempt, delivered twice by the HTTP transport."""
    await ws.agent_order_proposed_handler(_make_request(PAYLOAD, request_id="same-id"))
    replay = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, request_id="same-id")
    )

    assert replay.status == 200
    assert json.loads(replay.body)["success"] is True

    ws.bot_app.bot.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
async def test_send_failure_releases_both_dedup_layers_so_the_retry_gets_through(ws):
    """If the Telegram send fails AFTER the dedup keys are claimed, neither
    layer may absorb the Celery retry.

    Unlike the delivered summary — whose backend caller passes no request id, so
    every attempt gets a fresh one — THIS route's X-Request-ID is STABLE across
    retries: `push_agent_order_confirmation` passes the confirmation row's own
    16-hex `request_id` column to `trigger_bot_webhook`, and the same id across
    attempts collapses to one notification BY DESIGN. So the retry arrives under
    the very id layer 1 already claimed. Releasing only the order key still
    loses the proposal for good — the store never sees it and
    `sales.expire_agent_order_confirmations` auto-confirms an order nobody
    agreed to. This replays the id production really re-sends.
    """
    ws.bot_app.bot.send_message = AsyncMock(
        side_effect=[RuntimeError("telegram down"), None]
    )
    stable_request_id = PAYLOAD["request_id"]

    r1 = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, request_id=stable_request_id)
    )
    assert r1.status == 500

    r2 = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, request_id=stable_request_id)
    )
    assert r2.status == 200

    assert ws.bot_app.bot.send_message.await_count == 2


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_render_failure_releases_both_dedup_layers_so_the_retry_gets_through(
    ws, monkeypatch
):
    """The claims must not outlive a failure BEFORE the send, either.

    Everything between the two claims and the send can raise: the language read
    is a Postgres round-trip, and rendering/keyboard-building run on
    backend-supplied data. If that window sits outside the release-on-failure
    `try`, the 500 leaves BOTH keys held, and the Celery retry — carrying the
    same stable `X-Request-ID` (the confirmation row's own id) — is answered
    "already processed". The proposal is then lost for good and
    `sales.expire_agent_order_confirmations` auto-confirms an order the store
    never saw. One transient DB blip must cost one attempt, not the proposal.
    """
    monkeypatch.setattr(
        ws_module.i18n,
        "get_user_language",
        AsyncMock(side_effect=[RuntimeError("postgres blip"), "en"]),
    )
    stable_request_id = PAYLOAD["request_id"]

    r1 = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, request_id=stable_request_id)
    )
    assert r1.status == 500
    assert ws.bot_app.bot.send_message.await_count == 0

    r2 = await ws.agent_order_proposed_handler(
        _make_request(PAYLOAD, request_id=stable_request_id)
    )
    assert r2.status == 200
    assert json.loads(r2.body)["message"] == "Notification sent"
    assert ws.bot_app.bot.send_message.await_count == 1


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_payloads_own_request_id_dedups_when_the_header_is_missing(ws):
    """The push carries `request_id` in the BODY as well as the header —
    business_app/tasks/sales_agent_tasks.py::push_agent_order_confirmation hands
    the confirmation row's own id to `trigger_bot_webhook`, which puts it in
    X-Request-ID (business_app/utils/bot_webhook.py:75-81) and leaves it in the
    payload. This handler's docstring documents the body field and then reads
    only the header, so anything between the two services that drops an unknown
    header (a proxy, a rewritten route) silently costs layer 1 — the layer that
    collapses THIS route's Celery retries, because its id is stable across them.

    Two order ids stand in for the two pushes only to isolate layer 1: layer 2
    keys on `order:<id>` and would suppress a same-order replay by itself,
    hiding whether layer 1 fired at all.
    """
    first = dict(PAYLOAD, order_id=9101)
    second = dict(PAYLOAD, order_id=9102)

    r1 = await ws.agent_order_proposed_handler(_make_request(first, request_id=None))
    assert r1.status == 200

    r2 = await ws.agent_order_proposed_handler(_make_request(second, request_id=None))
    assert r2.status == 200
    assert json.loads(r2.body)["message"] == "Already processed (deduplicated)"

    ws.bot_app.bot.send_message.assert_awaited_once()
