"""Bot webhook route: POST /internal/customer-notice (plan 2026-10-24 Task 3; spec 2026-10-23 §4.4, §4.5, §9).

The customer notices the backend owes after a commit, rendered by the customer bot with its seeded copy and
buttons: a subscription order was created (with Pay now for an unpaid Click order, reusing `retry_payment`'s
`payment_retry_<id>`), a scheduled delivery was skipped, a paid order stays after skip/pause/cancel, and an
unpaid order was cancelled at its payment deadline.

Mirrors tests/telegram_bot/test_webhook_reorder_reminder.py, the precedent for every customer-bot webhook
route: real HMAC verification, the real Redis-backed dedup (one claim, on the backend's stable `notice_id`), a
deterministic i18n echo, the real `KeyboardBuilder`, a blocked bot answering 410 BOT_BLOCKED with the claim
released, and the copy rendered in the ONE language the backend resolved and sent (the route never reads the
customer's stored language).
"""

import hashlib
import hmac
import json
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

# Same two-step import as test_webhook_reorder_reminder.py: `import bot` claims the bot's bare deps in
# sys.modules; webhook_server.py is loaded BY FILE PATH under a unique name.
import bot  # noqa: F401

import importlib.util
from pathlib import Path

_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_customer_notice_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)
WebhookServer = ws_module.WebhookServer

# AFTER the bot import (scripts/seed_backend_translations.py inserts /app at sys.path[0]).
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402
from shared.redis_keyspace import RedisKeyspace  # noqa: E402

SECRET = "test-webhook-secret"
ENVELOPE = {"notice_id", "kind", "telegram_id", "language"}
# tests/integration/customer_notice_builders.py::CONTRACT_KEYS pins the backend's side.
CONTRACT_KEYS = {
    "subscription_order_created": ENVELOPE
    | {
        "order_id",
        "order_number",
        "delivery_date",
        "window_start",
        "window_end",
        "time_kind",
        "items",
        "not_included",
        "total",
        "payment",
        "amount_due",
        "prepaid_applied",
        "deadline",
    },
    "subscription_delivery_skipped": ENVELOPE | {"subscription_name", "skipped_dates", "reason", "next_delivery_date"},
    "subscription_paid_order_kept": ENVELOPE
    | {"order_id", "order_number", "delivery_date", "trigger", "next_delivery_date"},
    "order_payment_expired": ENVELOPE | {"order_id", "order_number", "deadline"},
    "subscription_schedule_changed": ENVELOPE
    | {
        "subscription_id",
        "subscription_name",
        "changes",
        "old_window",
        "new_window",
        "old_weekday",
        "new_weekday",
        "added_weekdays",
        "next_delivery_date",
        "time_kind",
    },
}

# One real push per kind, as business_app/services/customer_notices.py builds it, plus the `timestamp`
# business_app/utils/bot_webhook.py::trigger_bot_webhook adds to every body.
ORDER_CREATED = {
    "notice_id": "subscription-log:41",
    "kind": "subscription_order_created",
    "telegram_id": "810000001",
    "language": "en",
    "order_id": 9001,
    "order_number": "WB_000017_27",
    "delivery_date": "2027-01-07",
    "window_start": "10:00",
    "window_end": "12:00",
    "time_kind": "window",
    "items": [{"name": "Pure Water 19L", "qty": 2}, {"name": "Cooler cups", "qty": 1}],
    "not_included": [],
    "total": 30000.0,
    "payment": "cash",
    "amount_due": 30000.0,
    "prepaid_applied": None,
    "deadline": None,
    "timestamp": "2027-01-06T04:00:00+00:00",
}
DELIVERY_SKIPPED = {
    "notice_id": "subscription-log:42",
    "kind": "subscription_delivery_skipped",
    "telegram_id": "810000001",
    "language": "en",
    "subscription_name": "Office water",
    "skipped_dates": ["2027-01-07", "2027-01-14"],
    "reason": "a product was out of stock",
    "next_delivery_date": "2027-01-21",
    "timestamp": "2027-01-06T04:00:00+00:00",
}
PAID_ORDER_KEPT = {
    "notice_id": "subscription-log:43",
    "kind": "subscription_paid_order_kept",
    "telegram_id": "810000001",
    "language": "en",
    "order_id": 9001,
    "order_number": "WB_000017_27",
    "delivery_date": "2027-01-07",
    "trigger": "skip",
    "next_delivery_date": "2027-01-21",
    "timestamp": "2027-01-06T04:00:00+00:00",
}
PAYMENT_EXPIRED = {
    "notice_id": "order-payment-expired:9001",
    "kind": "order_payment_expired",
    "telegram_id": "810000001",
    "language": "en",
    "order_id": 9001,
    "order_number": "WB_000017_27",
    "deadline": "2027-01-07T12:00:00+05:00",
    "timestamp": "2027-01-07T07:15:00+00:00",
}
SCHEDULE_CHANGED = {
    "notice_id": "subscription-log:44",
    "kind": "subscription_schedule_changed",
    "telegram_id": "810000001",
    "language": "en",
    "subscription_id": 77,
    "subscription_name": "Office water",
    "changes": ["time_cleared", "day_moved"],
    "old_window": "10:00–12:00",
    "new_window": None,
    "old_weekday": 6,
    "new_weekday": 0,
    "added_weekdays": [],
    "next_delivery_date": "2027-01-11",
    "time_kind": "any_time",
    "timestamp": "2027-01-06T04:00:00+00:00",
}
ALL = [ORDER_CREATED, DELIVERY_SKIPPED, PAID_ORDER_KEPT, PAYMENT_EXPIRED, SCHEDULE_CHANGED]
CLICK = dict(ORDER_CREATED, payment="online_unpaid", amount_due=None, deadline="2027-01-07T12:00:00+05:00")

COPY_KEYS = sorted(k for k in BACKEND_TRANSLATIONS if k.startswith("telegram.customer_notice.")) + [
    "telegram.payment.pay_now",
    "telegram.payment.view_order",
] + [f"telegram.subscription.weekday_{day}" for day in range(7)]


class FakeRedis:
    """SET NX EX semantics, plus the TTL each key was given."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
                self.ttls.pop(key, None)
        return removed


def _make_request(payload, *, request_id="any-header-id", signature=None, signed=True):
    body = json.dumps(payload).encode("utf-8")
    if signature is None:
        signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Bot-Webhook-Signature": signature} if signed else {}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return SimpleNamespace(
        headers=headers,
        remote="10.0.0.5",
        path="/internal/customer-notice",
        read=AsyncMock(return_value=body),
        json=AsyncMock(return_value=payload),
    )


def _redis(ws):
    return ws.bot_app.bot_data["token_manager"].redis


@pytest.fixture
def ws():
    server = WebhookServer()
    server.bot_app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={"token_manager": SimpleNamespace(redis=FakeRedis())},
    )
    return server


@pytest.fixture(autouse=True)
def _webhook_env(monkeypatch):
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    # The route renders in the payload's `language` and never reads the customer's stored one: a read would
    # fail the request here (500), so every test below also pins that it never happens.
    monkeypatch.setattr(
        ws_module.i18n,
        "get_user_language",
        AsyncMock(side_effect=AssertionError("a customer notice renders in the payload's language")),
    )

    def fake_get(key, language, **kwargs):
        return "|".join([key, language] + [f"{k}={v}" for k, v in kwargs.items()])

    monkeypatch.setattr(ws_module.i18n, "get", fake_get)


def _use_real_i18n(monkeypatch):
    """Production's own get() over the SEEDED rows. The bot's own default language is uz, so a route that fell
    back to the bot's reading of the customer would show itself."""
    real = type(ws_module.i18n)
    monkeypatch.setattr(ws_module.i18n, "get", types.MethodType(real.get, ws_module.i18n))
    monkeypatch.setattr(ws_module.config.localization, "default_language", "uz")
    monkeypatch.setattr(
        ws_module.i18n,
        "translations",
        {language: {key: BACKEND_TRANSLATIONS[key][language] for key in COPY_KEYS} for language in ("en", "uz", "ru")},
    )


def _sent(ws):
    return ws.bot_app.bot.send_message.await_args.kwargs


def _buttons(kwargs):
    markup = kwargs["reply_markup"]
    if markup is None:
        return []
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize("payload", ALL, ids=lambda p: p["kind"])
def test_the_payloads_here_are_the_contract(payload):
    assert set(payload) - {"timestamp"} == CONTRACT_KEYS[payload["kind"]]


# --------------------------------------------------------------------------- the order notice


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_cash_order_names_the_date_window_items_total_and_the_door_payment(ws):
    resp = await ws.customer_notice_handler(_make_request(ORDER_CREATED))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["chat_id"] == "810000001"
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["text"] == (
        "telegram.customer_notice.order_created|en|order_number=WB_000017_27"
        "|delivery=telegram.customer_notice.delivery_window|en|date=07.01.2027|start=10:00|end=12:00"
        "|items=telegram.customer_notice.item|en|name=Pure Water 19L|qty=2"
        "\ntelegram.customer_notice.item|en|name=Cooler cups|qty=1"
        "|total=30,000"
        "|payment=telegram.customer_notice.pay_cash|en|amount=30,000"
    )
    assert _buttons(kwargs) == [("telegram.payment.view_order|en", "order_9001")]


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_left_out_product_is_named_after_the_item_lines(ws):
    payload = dict(ORDER_CREATED, items=[{"name": "Pure Water 19L", "qty": 2}], not_included=["Cooler <cups>"])

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    assert _sent(ws)["text"] == (
        "telegram.customer_notice.order_created|en|order_number=WB_000017_27"
        "|delivery=telegram.customer_notice.delivery_window|en|date=07.01.2027|start=10:00|end=12:00"
        "|items=telegram.customer_notice.item|en|name=Pure Water 19L|qty=2"
        "\ntelegram.customer_notice.not_included|en|name=Cooler &lt;cups&gt;"
        "|total=30,000"
        "|payment=telegram.customer_notice.pay_cash|en|amount=30,000"
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_unpaid_click_order_quotes_its_deadline_and_carries_pay_now(ws):
    resp = await ws.customer_notice_handler(_make_request(CLICK))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"].endswith("|payment=telegram.customer_notice.pay_online|en|deadline=01/07/2027, 12:00 PM")
    assert _buttons(kwargs) == [
        ("telegram.payment.pay_now|en", "payment_retry_9001"),
        ("telegram.payment.view_order|en", "order_9001"),
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_order_without_a_slot_reads_any_time(ws):
    payload = dict(ORDER_CREATED, window_start=None, window_end=None, time_kind="any_time")

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    assert "|delivery=telegram.customer_notice.delivery_any_time|en|date=07.01.2027|items=" in _sent(ws)["text"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_order_whose_slot_is_off_reads_time_to_be_confirmed(ws):
    payload = dict(ORDER_CREATED, window_start=None, window_end=None, time_kind="to_be_confirmed")

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    assert "|delivery=telegram.customer_notice.delivery_tbc|en|date=07.01.2027|items=" in _sent(ws)["text"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_old_shape_payload_without_time_kind_still_renders(ws):
    """A notice queued in Celery before the deploy has no `time_kind`: the old window rule decides."""
    old = {key: value for key, value in ORDER_CREATED.items() if key != "time_kind"}

    resp = await ws.customer_notice_handler(_make_request(dict(old, window_start=None, window_end=None)))

    assert resp.status == 200
    assert "|delivery=telegram.customer_notice.delivery_tbc|en|date=07.01.2027|items=" in _sent(ws)["text"]
    resp = await ws.customer_notice_handler(_make_request(dict(old, notice_id="subscription-log:99")))
    assert "|delivery=telegram.customer_notice.delivery_window|en|date=07.01.2027|start=10:00|end=12:00|" in _sent(ws)["text"]


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "facts, line",
    [
        (
            {"amount_due": 25000.0, "prepaid_applied": 5000.0},
            "telegram.customer_notice.pay_cash_prepaid|en|amount=5,000"
            "\ntelegram.customer_notice.pay_cash|en|amount=25,000",
        ),
        (
            {"amount_due": 0.0, "prepaid_applied": 30000.0},
            "telegram.customer_notice.pay_cash_prepaid|en|amount=30,000\ntelegram.customer_notice.pay_cash_covered|en",
        ),
        ({"payment": "company_account", "amount_due": None}, "telegram.customer_notice.pay_company_account|en"),
        ({"payment": "online_paid", "amount_due": None}, "telegram.customer_notice.pay_online_paid|en"),
    ],
    ids=["prepaid-part", "prepaid-all", "company-account", "online-paid"],
)
async def test_the_payment_line_follows_the_rail(ws, facts, line):
    resp = await ws.customer_notice_handler(_make_request(dict(ORDER_CREATED, **facts)))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"].endswith(f"|payment={line}")
    assert _buttons(kwargs) == [("telegram.payment.view_order|en", "order_9001")]  # Pay now only when unpaid online


# --------------------------------------------------------------------------- the other notices


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_skipped_delivery_says_when_why_and_what_comes_next(ws):
    resp = await ws.customer_notice_handler(_make_request(DELIVERY_SKIPPED))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"] == (
        "telegram.customer_notice.delivery_skipped|en|subscription_name=Office water"
        "|dates=07.01.2027, 14.01.2027|reason=a product was out of stock|next_date=21.01.2027"
    )
    assert _buttons(kwargs) == []


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "trigger, text",
    [
        (
            "skip",
            "telegram.customer_notice.paid_order_kept_skip|en|order_number=WB_000017_27|date=07.01.2027"
            "|next_date=21.01.2027",
        ),
        ("pause", "telegram.customer_notice.paid_order_kept_pause|en|order_number=WB_000017_27|date=07.01.2027"),
        ("cancel", "telegram.customer_notice.paid_order_kept_cancel|en|order_number=WB_000017_27|date=07.01.2027"),
    ],
)
async def test_a_paid_order_that_stays_is_told_per_action(ws, trigger, text):
    payload = dict(PAID_ORDER_KEPT, trigger=trigger, next_delivery_date="2027-01-21" if trigger == "skip" else None)

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"] == text
    assert _buttons(kwargs) == [("telegram.payment.view_order|en", "order_9001")]


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_order_cancelled_at_its_deadline_says_by_when(ws):
    resp = await ws.customer_notice_handler(_make_request(PAYMENT_EXPIRED))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"] == (
        "telegram.customer_notice.payment_expired|en|order_number=WB_000017_27|deadline=01/07/2027, 12:00 PM"
    )
    assert _buttons(kwargs) == [("telegram.payment.view_order|en", "order_9001")]


@pytest.mark.unit
@pytest.mark.anyio
async def test_backend_supplied_text_is_html_escaped(ws):
    payload = dict(ORDER_CREATED, order_number="A<b>", items=[{"name": "19L <XL> & co", "qty": 2}])
    skipped = dict(DELIVERY_SKIPPED, subscription_name="Home <i>", reason="stock <none>")

    await ws.customer_notice_handler(_make_request(payload))
    order_text = _sent(ws)["text"]
    await ws.customer_notice_handler(_make_request(skipped))
    skipped_text = _sent(ws)["text"]

    assert "order_number=A&lt;b&gt;" in order_text and "name=19L &lt;XL&gt; &amp; co" in order_text
    assert "subscription_name=Home &lt;i&gt;" in skipped_text and "reason=stock &lt;none&gt;" in skipped_text
    assert "<b>" not in order_text and "<i>" not in skipped_text


# --------------------------------------------------------------------------- the schedule changed (§6.5)


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_schedule_change_names_each_change_and_the_next_delivery(ws):
    payload = dict(
        SCHEDULE_CHANGED,
        subscription_name="Home <i>",
        changes=["time_cleared", "time_changed", "day_moved", "date_moved", "days_added"],
        old_window="10:00–12:00",
        new_window="11:00–13:00",
        added_weekdays=[5, 6],
    )

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["text"] == (
        "telegram.customer_notice.schedule_changed|en|subscription_name=Home &lt;i&gt;"
        "|lines=telegram.customer_notice.schedule_change.time_cleared|en|old_window=10:00–12:00"
        "\n\ntelegram.customer_notice.schedule_change.time_changed|en|new_window=11:00–13:00|old_window=10:00–12:00"
        "\n\ntelegram.customer_notice.schedule_change.day_moved|en"
        "|old_day=telegram.subscription.weekday_6|en|new_day=telegram.subscription.weekday_0|en"
        "\n\ntelegram.customer_notice.schedule_change.date_moved|en|old_day=telegram.subscription.weekday_6|en"
        "\n\ntelegram.customer_notice.schedule_change.days_added|en"
        "|days=telegram.subscription.weekday_5|en, telegram.subscription.weekday_6|en"
        "\n\ntelegram.customer_notice.schedule_changed_next|en|date=11.01.2027"
    )
    assert _buttons(kwargs) == []  # no order: no buttons (Task 17a adds Open subscription)


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_paused_subscription_change_has_no_next_delivery_line(ws):
    payload = dict(SCHEDULE_CHANGED, changes=["time_cleared"], next_delivery_date=None)

    resp = await ws.customer_notice_handler(_make_request(payload))

    assert resp.status == 200
    assert _sent(ws)["text"] == (
        "telegram.customer_notice.schedule_changed|en|subscription_name=Office water"
        "|lines=telegram.customer_notice.schedule_change.time_cleared|en|old_window=10:00–12:00"
    )


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"changes": ["moved"]},
        {"changes": []},
        {"changes": "time_cleared"},
        {"changes": None},
        {"changes": [["time_cleared"]]},
        {"added_weekdays": [7]},
        {"added_weekdays": ["6"]},
        {"added_weekdays": [True]},
        {"added_weekdays": None},
    ],
    ids=["unknown-code", "no-change", "not-a-list", "missing", "nested", "day-7", "day-text", "day-bool", "no-days"],
)
async def test_an_unknown_change_code_is_refused_before_the_claim(ws, change):
    resp = await ws.customer_notice_handler(_make_request(dict(SCHEDULE_CHANGED, **change)))

    assert resp.status == 400
    assert "Unknown kind" not in json.loads(resp.body)["message"]  # refused for its facts, not its kind
    ws.bot_app.bot.send_message.assert_not_awaited()
    assert _redis(ws).store == {}


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_english_schedule_notice_reads_as_written(ws, monkeypatch):
    _use_real_i18n(monkeypatch)

    resp = await ws.customer_notice_handler(_make_request(SCHEDULE_CHANGED))

    assert resp.status == 200
    assert _sent(ws)["text"] == (
        "🕒 <b>Office water</b>: your delivery schedule has changed.\n\n"
        "The 10:00–12:00 delivery time is no longer offered, so your deliveries now come at any time of the day. "
        "You can choose another time in your subscription.\n\n"
        "We no longer deliver on this day of the week (Sunday). Your new delivery day: Monday. "
        "You can change it in your subscription.\n\n"
        "📅 Next delivery: 11.01.2027"
    )


# --------------------------------------------------------------------------- refusals, dedup, failures


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"language": None},
        {"language": "ru-RU"},
        {"language": "fr"},
        {"kind": "subscription_renewal"},
        {"notice_id": None},
        {"telegram_id": None},
        {"kind": "subscription_paid_order_kept", "trigger": "resume"},
    ],
    ids=["no-language", "regional", "unsupported", "unknown-kind", "no-notice-id", "no-chat", "unknown-trigger"],
)
async def test_a_broken_payload_is_refused_before_the_claim(ws, change):
    base = PAID_ORDER_KEPT if change.get("kind") == "subscription_paid_order_kept" else ORDER_CREATED
    resp = await ws.customer_notice_handler(_make_request(dict(base, **change)))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()
    assert _redis(ws).store == {}


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_same_notice_is_sent_once_whatever_the_request_id(ws):
    """The backend's Celery retries carry the same `notice_id`; the claim is on it, held 24 hours."""
    await ws.customer_notice_handler(_make_request(ORDER_CREATED, request_id="attempt-1"))
    replay = await ws.customer_notice_handler(_make_request(ORDER_CREATED, request_id="attempt-2"))

    assert replay.status == 200
    assert json.loads(replay.body) == {"success": True, "message": "Already processed (deduplicated)"}
    ws.bot_app.bot.send_message.assert_awaited_once()
    assert _redis(ws).ttls == {RedisKeyspace.bot_webhook_dedup("customer-notice", "subscription-log:41"): 86_400}


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_blocked_bot_answers_410_and_releases_the_claim(ws):
    ws.bot_app.bot.send_message = AsyncMock(side_effect=[Forbidden("Forbidden: bot was blocked by the user"), None])

    blocked = await ws.customer_notice_handler(_make_request(ORDER_CREATED))
    assert blocked.status == 410
    assert json.loads(blocked.body) == {
        "success": False,
        "error_code": "BOT_BLOCKED",
        "message": "Bot was blocked by the user",
    }
    assert _redis(ws).store == {}

    retried = await ws.customer_notice_handler(_make_request(ORDER_CREATED))
    assert retried.status == 200
    assert ws.bot_app.bot.send_message.await_count == 2


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_send_failure_releases_the_claim_so_the_retry_gets_through(ws):
    ws.bot_app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])

    first = await ws.customer_notice_handler(_make_request(ORDER_CREATED))
    assert first.status == 500
    assert _redis(ws).store == {}
    retry = await ws.customer_notice_handler(_make_request(ORDER_CREATED))
    assert retry.status == 200
    assert ws.bot_app.bot.send_message.await_count == 2


@pytest.mark.unit
@pytest.mark.anyio
async def test_invalid_signature_returns_401(ws):
    resp = await ws.customer_notice_handler(_make_request(ORDER_CREATED, signature="deadbeef"))

    assert resp.status == 401
    ws.bot_app.bot.send_message.assert_not_awaited()
    assert _redis(ws).store == {}


@pytest.mark.unit
@pytest.mark.anyio
async def test_setup_registers_the_route(ws):
    await ws.setup()
    routes = {(r.method, r.resource.canonical) for r in ws.app.router.routes()}
    assert ("POST", "/internal/customer-notice") in routes


# --------------------------------------------------------------------------- the seeded copy, for real


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_english_order_notice_reads_as_written(ws, monkeypatch):
    _use_real_i18n(monkeypatch)

    resp = await ws.customer_notice_handler(_make_request(CLICK))

    assert resp.status == 200
    kwargs = _sent(ws)
    assert kwargs["text"] == (
        "🗓 <b>Your subscription order WB_000017_27 is placed</b>\n\n"
        "🚚 Delivery: 07.01.2027, from 10:00 to 12:00\n"
        "• Pure Water 19L × 2\n"
        "• Cooler cups × 1\n\n"
        "💰 Total: 30,000 UZS\n"
        "💳 Please pay by 01/07/2027, 12:00 PM with the 💳 Pay Now button. "
        "An order not paid by then is cancelled."
    )
    assert [text for text, _data in _buttons(kwargs)] == ["💳 Pay Now", "View Order"]


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("language", ["uz", "ru"])
@pytest.mark.parametrize("payload", ALL + [CLICK], ids=lambda p: f"{p['kind']}:{p.get('payment')}")
async def test_every_notice_renders_in_the_language_the_backend_resolved(ws, monkeypatch, language, payload):
    """No raw key, no `{placeholder}`, no "None", and the buttons in that language too."""
    _use_real_i18n(monkeypatch)

    resp = await ws.customer_notice_handler(_make_request(dict(payload, language=language)))

    assert resp.status == 200
    kwargs = _sent(ws)
    for leak in ("{", "}", "None", "telegram.customer_notice", "Customer notice"):
        assert leak not in kwargs["text"]
    for text, _data in _buttons(kwargs):
        assert (
            text
            == BACKEND_TRANSLATIONS[
                "telegram.payment.pay_now" if _data.startswith("payment_retry_") else "telegram.payment.view_order"
            ][language]
        )
    ws_module.i18n.get_user_language.assert_not_awaited()
