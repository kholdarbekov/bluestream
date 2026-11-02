"""Bot webhook route: POST /internal/payment-success keeps its dedup claim only for a confirmation it delivered
(step-4 live drive D1, 2026-11-02).

The backend sends one stable X-Request-ID per payment, `payment-confirm:<payment_id>`
(business_app/utils/payment_confirmation_dispatch.py), and the route claims it before the send. A send that failed
used to keep the claim: the backend un-marked the payment for its side-effect sweep, the sweep re-sent it, the bot
answered 200 "Already processed" and the backend counted a confirmation the customer never got. Now the route keeps
the claim the way /internal/customer-notice does:
- a send Telegram refused for a passing reason (chat not found, Bad Gateway) releases the claim and answers 500, so
  the sweep's re-send of the same request id is delivered; a repeat of that failure within a day is a warning line,
  only the first an error with its traceback;
- a send Telegram refused because the customer blocked the bot releases the claim and answers 410 BOT_BLOCKED, the
  backend's signal to mark the account blocked and take the email channel next;
- a delivered confirmation (a new message, or the stored payment message edited) keeps its claim for 24 hours, and the
  same request id again is "Already processed".

Driven through the real route (aiohttp test server, real HMAC) with the real customer bot of the dispatcher harness:
Telegram is its fake transport (`tests/bot_dispatcher_harness.py`), Redis an in-memory store.
"""

import hashlib
import hmac
import importlib
import importlib.util
import json
import logging
from pathlib import Path

import pytest
from aiohttp import test_utils

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness

# In a mixed xdist worker the bare `webhook_server` name belongs to the staff bot. Let `bot` claim the customer bot's
# bare deps first, then load the module by path under a unique name (tests/telegram_bot/test_payment_link_back.py).
importlib.import_module("bot")
_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_payment_success_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)

from keyboards import PaymentKeyboards  # noqa: E402  (the bot's module, after `bot` claimed its deps)
from shared.redis_keyspace import RedisKeyspace  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SECRET = "test-webhook-secret"
ROUTE = "/internal/payment-success"
REQUEST_ID = "payment-confirm:185"
# The body `PaymentService.dispatch_payment_confirmation` parks, plus the `timestamp` trigger_bot_webhook adds.
PAYLOAD = {
    "user_id": 109,
    "telegram_id": DEFAULT_USER_ID,
    "order_id": 205,
    "order_number": "WB_000019_26",
    "amount": 30000.0,
    "currency": "UZS",
    "timestamp": "2026-11-02T12:23:40+00:00",
}
CLAIM = RedisKeyspace.bot_webhook_dedup("payment-success", REQUEST_ID)
# A failed send is remembered for a day, so only its first failure is logged as an error.
FAILED_BEFORE = RedisKeyspace.bot_webhook_dedup("payment-success-failed", REQUEST_ID)
PAYMENT_MESSAGE = RedisKeyspace.bot_payment_message(PAYLOAD["order_id"])
SENT = {"success": True, "message": "Notification sent"}
FAILED = {"success": False, "message": "Internal server error"}
BLOCKED = {"success": False, "error_code": "BOT_BLOCKED", "message": "Bot was blocked by the user"}
ALREADY = {"success": True, "message": "Already processed (deduplicated)"}


class FakeRedis:
    """SET NX EX, GET and DELETE, with the TTL each key was given."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
                self.ttls.pop(key, None)
        return removed


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch)
    harness.application.bot_data["token_manager"].redis = FakeRedis()
    return harness


@pytest.fixture
def redis(bot):
    return bot.application.bot_data["token_manager"].redis


@pytest.fixture
async def push(bot, monkeypatch):
    """POST the backend's push, signed as `trigger_bot_webhook` signs it; answers (status, body)."""
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    server = ws_module.WebhookServer()
    server.set_application(bot.application)
    await server.setup()

    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:

        async def _push(request_id=REQUEST_ID):
            raw = json.dumps(PAYLOAD).encode("utf-8")
            signature = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            response = await client.post(
                ROUTE,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Bot-Webhook-Signature": signature,
                    "X-Request-ID": request_id,
                },
            )
            return response.status, await response.json()

        yield _push


def _keyboard(language="uz"):
    markup = PaymentKeyboards.payment_success(PAYLOAD["order_id"], language)
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _sends(bot):
    """Every sendMessage the bot made: (chat, buttons, what Telegram refused it with, or None)."""
    return [(call.params["chat_id"], call.callback_data(), call.refused) for call in bot.telegram.of("sendMessage")]


async def test_a_delivered_confirmation_keeps_its_claim_for_a_day_and_a_replay_is_already_processed(bot, redis, push):
    assert await push() == (200, SENT)
    assert await push() == (200, ALREADY)

    assert _sends(bot) == [(DEFAULT_USER_ID, _keyboard(), None)]
    assert (redis.store, redis.ttls) == ({CLAIM: "1"}, {CLAIM: 86_400})


async def test_the_payment_message_on_screen_is_edited_and_the_claim_kept(bot, redis, push):
    redis.store[PAYMENT_MESSAGE] = "5077"

    assert await push() == (200, SENT)

    (edit,) = bot.telegram.of("editMessageText")
    assert (edit.params["chat_id"], edit.params["message_id"], edit.refused) == (DEFAULT_USER_ID, 5077, None)
    assert edit.callback_data() == _keyboard()
    assert bot.telegram.of("sendMessage") == []
    assert redis.store == {CLAIM: "1"}  # the stored message id is spent; the claim is held
    assert await push() == (200, ALREADY)


@pytest.mark.parametrize(
    "status, description",
    [(400, "Bad Request: chat not found"), (502, "Bad Gateway")],
    ids=["chat-not-found", "bad-gateway"],
)
async def test_a_send_that_failed_releases_its_claim_and_the_sweeps_re_send_is_delivered(
    bot, redis, push, status, description
):
    """The live drive's 17:23 push failed with "Chat not found"; the 17:37 sweep's re-send of the same request id was
    answered "Already processed" and counted as delivered."""
    bot.telegram.fail("sendMessage", description, status=status)

    assert await push() == (500, FAILED)
    assert (redis.store, redis.ttls) == ({FAILED_BEFORE: "1"}, {FAILED_BEFORE: 86_400})  # the claim is released

    bot.telegram.clear_failures()
    assert await push() == (200, SENT)

    assert _sends(bot) == [(DEFAULT_USER_ID, _keyboard(), description), (DEFAULT_USER_ID, _keyboard(), None)]
    assert CLAIM in redis.store


async def test_a_send_that_fails_again_on_the_sweeps_re_send_is_a_warning_without_a_traceback(
    bot, redis, push, caplog
):
    """The backend's sweep re-sends a confirmation the bot could not send on every run, for days ("chat not found"
    stays a passing failure, owner call). Its first failure is the error, with the traceback; each repeat within the
    day is one warning line."""
    bot.telegram.fail("sendMessage", "Bad Request: chat not found", status=400)
    caplog.set_level(logging.INFO, logger=ws_module.logger.name)

    assert [await push() for _ in range(3)] == [(500, FAILED)] * 3

    logged = [
        (record.levelno, record.getMessage(), record.exc_info is not None)
        for record in caplog.records
        if record.name == ws_module.logger.name and record.levelno >= logging.WARNING
    ]
    again = (logging.WARNING, f"Payment confirmation {REQUEST_ID} not delivered again: Chat not found", False)
    assert logged == [(logging.ERROR, "Error processing payment success: Chat not found", True), again, again]
    assert CLAIM not in redis.store


@pytest.mark.parametrize(
    "description",
    ["Forbidden: bot was blocked by the user", "Forbidden: user is deactivated"],
    ids=["blocked", "deactivated"],
)
async def test_a_customer_who_blocked_the_bot_is_answered_410_bot_blocked_and_no_claim_is_left(
    bot, redis, push, description
):
    """The customer-notice route's answer for the same refusal: the backend marks the account blocked and takes the
    email channel next. The bot never holds a claim for a confirmation it could not deliver."""
    bot.telegram.fail("sendMessage", description, status=403)

    assert await push() == (410, BLOCKED)
    assert redis.store == {}
    assert await push() == (410, BLOCKED)  # no claim held over from the first: answered the same, not deduplicated

    assert _sends(bot) == [(DEFAULT_USER_ID, _keyboard(), description)] * 2


async def test_a_blocked_customer_whose_payment_message_cannot_be_edited_is_answered_410_too(bot, redis, push):
    """The edit is tried first and refused, then the new message is refused the same way."""
    redis.store[PAYMENT_MESSAGE] = "5077"
    bot.telegram.fail("editMessageText", "Forbidden: bot was blocked by the user", status=403)
    bot.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", status=403)

    assert await push() == (410, BLOCKED)

    assert [call.refused for call in bot.telegram.of("editMessageText", "sendMessage")] == [
        "Forbidden: bot was blocked by the user"
    ] * 2
    assert CLAIM not in redis.store
